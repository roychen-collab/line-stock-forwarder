"""只查看本機執行鎖和進度，不操作 LINE、不連網、不發送。"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def lock_is_held(path):
    import msvcrt
    if not path.exists():
        return False
    with path.open('r+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno != 13:
                raise RuntimeError('無法確認執行鎖狀態。') from None
            return True
        else:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            return False


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    import msvcrt
    logs = ROOT / 'logs'
    lock_path = logs / 'forward-test.lock'
    running = False
    if lock_path.exists():
        with lock_path.open('r+b') as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno != 13:
                    raise RuntimeError('無法確認執行鎖狀態。') from None
                running = True
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    reading_only = running and lock_is_held(logs/'formal-readonly.lock')
    if reading_only:
        print('唯讀測試：正在執行；未啟動轉傳')
    else:
        print('轉傳程式：' + ('正在執行' if running else '已停止'))
    readonly_path = logs/'formal-readonly-state.json'
    if readonly_path.exists():
        readonly = json.loads(readonly_path.read_text(encoding='utf-8'))
        print('唯讀測試紀錄：' + str(readonly.get('status', '未知')))
        print('唯讀成功擷取：' + str(readonly.get('copy_checks', 0)) + ' 次；新增區塊：'
              + str(readonly.get('changed_snapshots', 0)))
        if readonly.get('error'):
            print('唯讀最近錯誤：' + readonly['error'])
    state_path = logs / 'forward-test-state.json'
    if state_path.exists() and not reading_only:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        print('最近紀錄：' + str(state.get('status', '未知')))
        print('測試群組：' + str(state.get('group', '尚未設定')))
        print('本輪已送出：' + str(state.get('sent_count', 0)) + ' 則')
        if state.get('skipped_sender_count'):
            print('本輪略過指定發送者：' + str(state['skipped_sender_count']) + ' 則')
        if not running and state.get('status') in ('starting', 'watching', 'preparing', 'sending', 'capturing_image', 'paused'):
            print('前次可能異常中斷；此紀錄不表示仍在監控。')
        if state.get('pending') is not None:
            print('有待確認結果：先查看手機及 log，不要重送或刪除紀錄。')
        if state.get('failed_message') is not None and state.get('send_attempt_started') is False:
            print('前次在準備階段停止，尚未嘗試發送；未完成訊息仍保留，需先排查錯誤。')
        if state.get('error'):
            print('最近錯誤：' + state['error'])
    progress_path = logs / 'forward-progress.json'
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding='utf-8'))
        print('保存進度：已處理第 ' + str(progress.get('acknowledged_count')) + ' 則以前的內容')
        if progress.get('pending') is not None:
            print('保存進度有待確認發送；程式會禁止自動重送。')
    else:
        print('保存進度：尚未建立')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'狀態檢查失敗：{exc}；未操作 LINE 或發送。')
        raise SystemExit(1)
