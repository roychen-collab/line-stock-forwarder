"""限時觀察 LINE 正常複製內容；不轉傳、不搶焦點、不解析未確認的姓名。"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time

from clipboard_probe import (run_capture, appended_text, FocusChangedError,
                             ClipboardNotUpdatedError)

ROOT = Path(__file__).resolve().parent


def readiness(group, idle_seconds):
    """只查看公開前景資訊及最近輸入時間，沒有輸入或視窗操作。"""
    import win32gui
    import win32process
    import psutil
    handle = win32gui.GetForegroundWindow()
    if not handle or win32gui.GetWindowText(handle) != group:
        return 'source_not_foreground'
    if win32gui.IsIconic(handle):
        return 'source_minimized'
    pid = win32process.GetWindowThreadProcessId(handle)[1]
    executable = Path(psutil.Process(pid).exe()).as_posix().lower()
    if not executable.endswith('/line/bin/current/line.exe'):
        raise RuntimeError('來源不是已核對的官方 LINE 執行檔，禁止複製。')

    class LastInputInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise RuntimeError('無法確認使用者輸入狀態，禁止複製。')
    elapsed = ((ctypes.windll.kernel32.GetTickCount64() & 0xffffffff) - info.dwTime) & 0xffffffff
    return 'user_active' if elapsed < idle_seconds * 1000 else None


class RawCheckpoint:
    """只確認完整原始前綴，保存新增區塊；不把媒體提示猜成文字訊息。"""
    def __init__(self, path, group):
        self.path, self.group = path, group
        self.state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if self.state is not None and (self.state.get('version') != 1
                                       or self.state.get('group') != group):
            raise ValueError('唯讀保存群組不同，禁止覆蓋。')

    def observe(self, transcript, snapshot):
        added = '' if self.state is None else appended_text(self.state['transcript'], transcript)
        state = {'version': 1, 'group': self.group, 'transcript': transcript,
                 'snapshot': str(snapshot), 'send_attempts': 0,
                 'time': datetime.now().astimezone().isoformat()}
        atomic_save(self.path, state)
        self.state = state
        return added


def atomic_save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def observe_once(checkpoint, snapshot, group, idle_seconds, reader=run_capture, gate=readiness):
    reason = gate(group, idle_seconds)
    if reason:
        return {'paused': reason, 'copied': False}
    report = reader(snapshot, group, activate=False)
    added = checkpoint.observe(report['text'], snapshot)
    return {'paused': None, 'copied': True, 'added': added,
            'characters': len(report['text'])}


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', required=True, help='已確認的獨立來源聊天室完整名稱')
    parser.add_argument('--duration', type=int, default=1200, help='60～3600 秒，預設20分鐘')
    parser.add_argument('--interval', type=int, default=10, help='至少5秒，預設10秒')
    parser.add_argument('--idle-seconds', type=int, default=3, help='至少2秒無使用者輸入才可讀取')
    args = parser.parse_args()
    if (not args.group.strip() or args.group == 'LINE' or not 60 <= args.duration <= 3600
            or args.interval < 5 or args.idle_seconds < 2):
        parser.error('群組或限時／檢查間隔／閒置秒數不符合限制。')
    logs = ROOT/'logs'
    logs.mkdir(exist_ok=True)
    run = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
    handler = logging.FileHandler(logs/f'readonly-{run}.log', encoding='utf-8')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[handler, logging.StreamHandler(sys.stdout)], force=True)
    state_path = logs/'formal-readonly-state.json'
    state = {'run': run, 'group': args.group, 'status': 'starting', 'dry_run': True,
             'send_attempts': 0, 'copy_checks': 0, 'changed_snapshots': 0,
             'focus_pauses': 0, 'input_pauses': 0,
             'ends_at': (datetime.now().astimezone()+timedelta(seconds=args.duration)).isoformat()}
    locks = []
    owns_lock = False

    def save(status, **fields):
        state.update(status=status, time=datetime.now().astimezone().isoformat(), **fields)
        atomic_save(state_path, state)

    try:
        import msvcrt
        for filename in ['forward-test.lock', 'formal-readonly.lock']:
            lock = (logs/filename).open('a+b')
            locks.append(lock)
            if lock.seek(0, 2) == 0:
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        owns_lock = True
        checkpoint = RawCheckpoint(logs/'formal-readonly-checkpoint.json', args.group)
        deadline = time.monotonic()+args.duration
        failures = 0
        save('watching')
        logging.info('唯讀觀察開始；群組=%s；%s 秒；不發送、不搶焦點。', args.group, args.duration)
        while time.monotonic() < deadline:
            snapshot = logs/f'readonly-{run}-copy-{state["copy_checks"]+1}.json'
            try:
                result = observe_once(checkpoint, snapshot, args.group, args.idle_seconds)
            except FocusChangedError:
                result = {'paused': 'focus_changed_during_capture', 'copied': False}
            except ClipboardNotUpdatedError:
                failures += 1
                if failures >= 2:
                    raise RuntimeError('連續兩次剪貼簿未更新，唯讀測試停止；未使用舊內容。')
                result = {'paused': 'clipboard_not_updated', 'copied': False}
            if result['paused']:
                counter = 'input_pauses' if result['paused'] == 'user_active' else 'focus_pauses'
                state[counter] += 1
                if state.get('reason') != result['paused']:
                    logging.info('暫停讀取：%s。', result['paused'])
                save('paused', reason=result['paused'])
            else:
                failures = 0
                state['copy_checks'] += 1
                if result['added']:
                    event_path = logs/f'readonly-{run}-change-{state["changed_snapshots"]+1}.json'
                    atomic_save(event_path, {'group': args.group, 'snapshot': str(snapshot),
                                             'added_text': result['added'], 'sent': False})
                    state['changed_snapshots'] += 1
                    state['last_change_path'] = str(event_path)
                    logging.info('偵測到新增原始區塊；%s 字元；未解析媒體或發送。', len(result['added']))
                save('watching', reason=None, characters=result['characters'])
            time.sleep(min(args.interval, max(0, deadline-time.monotonic())))
        save('completed', reason='time_limit', test_observed_content=bool(state['copy_checks']))
        logging.info('唯讀觀察結束；成功擷取=%s；新增區塊=%s；發送=0。',
                     state['copy_checks'], state['changed_snapshots'])
        return 0
    except KeyboardInterrupt:
        if owns_lock:
            save('stopped', reason='keyboard_interrupt')
        return 130
    except Exception as exc:
        if owns_lock:
            save('error', error=str(exc))
        logging.error('唯讀測試停止：%s', exc)
        return 1
    finally:
        if owns_lock:
            atomic_save(logs/f'readonly-{run}-final-state.json', state)
        for lock in reversed(locks):
            lock.close()
        handler.close()


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
