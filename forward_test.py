"""限時的 LINE 測試文字 → 已配對 Telegram；略過舊訊息，重複讀取失敗或發送不明即停止。"""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time

from chat_text import UnsupportedRecallError, new_messages, parse_transcript
from clipboard_probe import ClipboardNotUpdatedError, FocusChangedError, run_capture
from image_capture import run_image_capture
from forward_progress import ForwardProgress
from sender_policy import ignored_senders_for_group
from telegram_client import TelegramError, call_api, load_token, send_text, send_image

ROOT = Path(__file__).resolve().parent
# 在複製文字缺乏媒體型別時保守拒絕這些提示文字，並非完整的 LINE 媒體辨識。
RESERVED_MEDIA_TEXTS = {"貼圖", "影片", "語音", "語音訊息", "檔案", "[貼圖]", "[影片]", "[語音]", "[檔案]"}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=600, help="測試秒數，預設／最多 600 秒")
    parser.add_argument("--max-messages", type=int, default=4, help="最多轉傳幾則，最多 10 則")
    parser.add_argument("--recover-last-test", action="store_true", help="只補送今天最後一則符合前綴的測試文字；有一次性紀錄，不監控")
    parser.add_argument("--expected-recovery", type=Path, help="補送前要求最新訊息與此 JSON 的 day／clock／sender／text 完全一致")
    parser.add_argument("--include-images", action="store_true", help="實驗：允許一輪單張圖片，需 OCR 身分核對；同分鐘歧義或混合批次停止")
    parser.add_argument("--hold-for-test", action="store_true", help="建立起點後暫停讀取，待本輪 ready 檔出現才繼續；只供收回提示受控測試")
    parser.add_argument("--allow-normal-text", action="store_true", help="限時測試群模式：接受已設定帳號的一般文字，不要求測試前綴；不適用正式群")
    parser.add_argument('--resume-progress', action='store_true', help='限時測試：首次略過舊內容並保存進度，之後核對原進度續傳；結果不明停止')
    args = parser.parse_args()
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    handlers = [logging.FileHandler(logs / f"forward-test-{stamp}.log", encoding="utf-8"),
                logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)
    state_path = logs / "forward-test-state.json"
    snapshot = logs / f"forward-test-{stamp}-snapshot.json"
    state = {"status": "starting", "run": stamp, "sent_count": 0,
             "send_attempt_started": False}
    lock = None
    owns_lock = False
    progress = None

    def save(status, **fields):
        state.update(status=status, time=datetime.now().astimezone().isoformat(), **fields)
        temporary = state_path.with_suffix(".tmp")
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)

    def preserve_failure():
        # 只清除本輪明確尚未呼叫 Telegram 的待處理標記。
        # 發送已開始或旗標缺失時仍保留 pending，禁止自動重送。
        if state.get('pending') is not None and state.get('send_attempt_started') is False:
            state.update(failed_message=state['pending'],
                         failed_message_kind=state.get('pending_kind'),
                         pending=None, pending_kind=None)
            logging.info('本則在準備階段停止，尚未嘗試 Telegram 發送；保存未完成內容，不自動重試。')

    try:
        import msvcrt
        lock = (logs / "forward-test.lock").open("a+b")
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("已有轉傳測試執行中，不能同時開啟第二份。") from None
        owns_lock = True
        if state_path.exists():
            prior = json.loads(state_path.read_text(encoding='utf-8'))
            if prior.get('pending') is not None:
                owns_lock = False  # 保留前次 pending，錯誤也不覆蓋原始證據。
                raise ValueError('前次有未確認發送；先查看手機及 log，禁止重送或覆蓋前次狀態。')
        progress_path = logs / 'forward-progress.json'
        if progress_path.exists() and not args.resume_progress:
            owns_lock = False
            raise ValueError('已有保存進度；請使用 --resume-progress，不允許另建起點繞過紀錄。')
        if args.resume_progress and (args.recover_last_test or args.hold_for_test):
            raise ValueError('保存進度續傳不能搭配歷史補送或收回提示受控等待。')
        if not 1 <= args.duration <= 600 or not 1 <= args.max_messages <= 10:
            raise ValueError("測試需限制為 1～600 秒、1～10 則。")
        if args.include_images and args.recover_last_test:
            raise ValueError("圖片實驗不補送歷史圖片；不能搭配 --recover-last-test。")
        if args.hold_for_test and args.recover_last_test:
            raise ValueError("收回提示受控等待不能搭配補送模式。")
        if args.allow_normal_text and args.recover_last_test:
            raise ValueError("一般文字受控測試不能搭配歷史補送。")
        if args.expected_recovery is not None and not args.recover_last_test:
            raise ValueError("--expected-recovery 必須搭配 --recover-last-test。")
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        group, senders = config.get("test_group"), config.get("test_senders")
        if not isinstance(senders, list) or not senders:
            raise ValueError('需先設定已核對的成員顯示名稱。')
        ignored_senders = ignored_senders_for_group(config, group, senders)
        if ignored_senders and args.recover_last_test:
            raise ValueError('略過發送者的模式不支援歷史補送。')
        prefix = config.get("test_message_prefix")
        skip_recalls = config.get("skip_recall_notices", False)
        if type(skip_recalls) is not bool:
            raise ValueError("skip_recall_notices 必須是 true 或 false。")
        if not isinstance(group, str) or not group.strip() or group == "LINE" or group == config.get("target_group"):
            raise ValueError("test_group 不可空白、不可為 LINE 主視窗或 LINE 目標群。")
        if args.allow_normal_text and group == config.get("source_group"):
            raise ValueError("一般文字受控測試只能使用測試群，不能使用正式來源群。")
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("需設定 test_message_prefix，禁止無限制轉傳。")
        interval = config.get("polling_interval", 5)
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 2:
            raise ValueError("polling_interval 必須至少為 2 秒的有限數字。")
        target = config["telegram"]
        chat_id, bot_id = target["chat_id"], target["bot_id"]
        if type(chat_id) is not int or chat_id <= 0 or type(bot_id) is not int:
            raise ValueError("接收端須為已配對的私人聊天室。")
        if target["credential_target"] != f"line-stock-forwarder/telegram/{bot_id}":
            raise ValueError("Bot 認證與設定不相符。")
        paired = json.loads((logs / "telegram-setup-state.json").read_text(encoding="utf-8"))
        if (paired.get("status") != "completed" or paired.get("chat_id") != chat_id
                or paired.get("bot_username") != target["bot_username"]):
            raise ValueError("接收端與成功配對紀錄不相符。")
        token = load_token(target["credential_target"])
        bot = call_api(token, "getMe")
        if not bot.get("is_bot") or bot.get("id") != bot_id or bot.get("username") != target["bot_username"]:
            raise ValueError("Bot 身分驗證與配對設定不相符。")
        save("starting", group=group, chat_id=chat_id, prefix=prefix, require_test_prefix=not args.allow_normal_text)
        logging.info("受控轉傳測試開始；群組=%s；前綴=%s；期限=%s 秒；上限=%s 則。", group, prefix, args.duration, args.max_messages)
        if args.allow_normal_text:
            logging.info("一般文字受控模式：不要求前綴，仍核對測試群與設定的發送者，受期限／則數限制；媒體型別未全面辨識。")
        first_snapshot = run_capture(snapshot, group, activate=True)["text"]
        previous = first_snapshot
        queued_snapshot = None
        if args.resume_progress:
            context = {'group': group, 'chat_id': chat_id, 'bot_id': bot_id,
                       'senders': senders, 'skip_recalls': skip_recalls, 'prefix': prefix,
                       'require_test_prefix': not args.allow_normal_text, 'include_images': args.include_images}
            if ignored_senders:
                context['ignored_senders'] = ignored_senders
            progress = ForwardProgress(progress_path, context, first_snapshot)
            previous = progress.state['transcript']
            if progress.resumed:
                queued_snapshot = first_snapshot
                logging.info('已核對保存進度；已處理位置=%s，先檢查停止期間的新增內容。',
                             progress.state['acknowledged_count'])
            else:
                logging.info('首次保存進度；只記錄目前舊内容，不發送歷史。')
        baseline_notices = []
        baseline = parse_transcript(previous, senders, skip_recall_notices=skip_recalls, notices=baseline_notices)
        if skip_recalls:
            logging.warning("已啟用固定格式收回提示跳過；同格式的多行正文可能誤判；舊歷史改写仍停止。")
        for notice in baseline_notices:
            logging.info("略過既有收回提示；LINE 時間=%s %s；sender=%s；行=%s。",
                         notice.day, notice.clock, notice.sender, notice.line_number)
        ignored_recalls = len(baseline_notices)
        if args.recover_last_test:
            if baseline_notices or not baseline:
                raise ValueError("聊天室含收回提示或沒有文字，不支援補送模式；禁止猜測最新訊息。")
            message = baseline[-1]
            if (message.day != datetime.now().astimezone().date().isoformat()
                    or not message.text.startswith(prefix)
                    or len(message.text.encode("utf-16-le")) // 2 > 4096):
                raise ValueError("最新一則不是今天約定的測試文字，禁止補送。")
            if args.expected_recovery is not None:
                expected = json.loads(args.expected_recovery.read_text(encoding="utf-8-sig"))
                if asdict(message) != expected:
                    raise ValueError("最新訊息與指定補送的訊息身分／內容不符，禁止發送。")
            key = json.dumps({"group": group, "chat_id": chat_id, **asdict(message)},
                             ensure_ascii=False, sort_keys=True)
            marker = logs / f"forward-recovery-{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
            if marker.exists():
                raise ValueError("這則測試已有補送嘗試紀錄；不重複發送，請查看手機及 log。")
            recovery = {"status": "attempting", "time": datetime.now().astimezone().isoformat(),
                        "group": group, "chat_id": chat_id, "message": asdict(message)}
            with marker.open("x", encoding="utf-8") as handle:
                json.dump(recovery, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            save("sending", pending=asdict(message), recovery=True, send_attempt_started=True)
            logging.info("只補送最新測試文字一次；LINE 時間=%s %s；sender=%s；文字=%s", message.day, message.clock, message.sender, message.text)
            result = send_text(token, chat_id, message.text)
            recovery.update(status="sent", message_id=result["message_id"])
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(json.dumps(recovery, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, marker)
            save("completed", sent_count=1, pending=None, send_attempt_started=False,
                 last_message_id=result["message_id"], reason="single_recovery")
            logging.info("補送成功；Telegram message_id=%s；已停止，未補發其他歷史。", result["message_id"])
            return 0
        if progress:
            logging.info('已核對 %s 則觀測內容；已處理位置=%s；其後未完成內容仍待檢查，不略過。LINE 必須保持前景。',
                         len(baseline), progress.state['acknowledged_count'])
        else:
            logging.info("建立起始位置，略過 %s 則舊內容；LINE 必須保持前景。", len(baseline))
        if args.include_images:
            logging.info("圖片實驗模式：只處理單張、最後一則且同分鐘身分唯一的圖片；混合批次或任何 OCR 核對失敗即停止。")
        save("watching", baseline_count=len(baseline), has_new=False, skip_recall_notices=skip_recalls,
             ignored_recall_count=ignored_recalls, resume_progress=args.resume_progress,
             acknowledged_count=progress.state['acknowledged_count'] if progress else None)
        deadline = time.monotonic() + args.duration
        if args.hold_for_test:
            ready_path = logs / f"forward-test-{stamp}.ready"
            save("paused", reason="test_hold", test_ready_file=str(ready_path))
            logging.info("收回提示受控測試：已保存起點，暫停讀取及發送；等本輪 ready 檔才繼續，等待仍計入期限。")
            while not ready_path.exists():
                if time.monotonic() >= deadline:
                    save("completed", reason="time_limit")
                    logging.info("受控等待到期，沒有讀取新的訊息，也沒有發送。")
                    return 0
                time.sleep(0.25)
            logging.info("本輪受控等待已解除；保留原起點，核對新增收回提示及文字。")
        next_check = time.monotonic() + (0 if queued_snapshot is not None else interval)
        sent_count = 0
        skipped_sender_count = 0
        copy_failures = 0
        while time.monotonic() < deadline:
            time.sleep(min(max(0, next_check - time.monotonic()), max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            check_started = time.monotonic()
            # 從本輪開始計時，讓正常讀取耗時包含在間隔內；慢操作後不累積補跑。
            next_check = check_started + interval
            try:
                if queued_snapshot is not None:
                    current, queued_snapshot = queued_snapshot, None
                else:
                    current = run_capture(snapshot, group, activate=False)["text"]
            except ClipboardNotUpdatedError as exc:
                copy_failures += 1
                save("paused", reason="clipboard_not_updated", copy_failures=copy_failures)
                if copy_failures >= 2:
                    raise ClipboardNotUpdatedError("連續兩次複製都沒有新剪貼簿內容，整輪停止；沒有使用舊剪貼簿。") from exc
                # 只有讀取可再檢查一次。保留原基準、不發送、不在本次擷取中連按。
                next_check = time.monotonic() + interval
                logging.warning("本次複製沒有更新；保留原進度，至少 %s 秒後只再讀取一次；再次失敗就停止。", interval)
                continue
            except FocusChangedError as exc:
                if state["status"] != "paused" or state.get("reason") != exc.error_code:
                    logging.info("LINE 暫時無法讀取：%s；暫停複製與發送，保留原進度。"
                                 "請手動登入／開啟來源聊天室並回到前景；不自動操作登入或視窗。", exc)
                save("paused", reason=exc.error_code)
                continue
            if state["status"] == "paused":
                logging.info("來源已可讀取，繼續核對原起始位置。")
            copy_failures = 0
            new_notices = []
            batch = (progress.observe(current, notices=new_notices) if progress else
                     new_messages(previous, current, senders, skip_recall_notices=skip_recalls, notices=new_notices))
            ignored_recalls += len(new_notices)
            for notice in new_notices:
                logging.info("略過新增收回提示；LINE 時間=%s %s；sender=%s；行=%s；繼續核對下一則。",
                             notice.day, notice.clock, notice.sender, notice.line_number)
            save("watching", ignored_recall_count=ignored_recalls)
            to_send = [message for message in batch if message.sender not in ignored_senders]
            # 整批先驗證再送；不因未知系統事件／媒體文字而誤發。
            has_image = any(message.text == "圖片" for message in to_send)
            if has_image and not args.include_images:
                raise ValueError("LINE 新內容包含「圖片」提示，但複製文字沒有可靠媒體類型，"
                                 "且本機 UIA 無法核對該圖片的訊息身分；整批停止且未發送。"
                                 "請查看 image-probe、image-send-test 與 selection 探測紀錄")
            if has_image and len(batch) != 1:
                raise ValueError("圖片實驗僅接受本輪單張新增圖片；多張或文字圖片混合批次整批停止，未發送。")
            if args.allow_normal_text and any(message.text.strip() in RESERVED_MEDIA_TEXTS for message in to_send):
                raise ValueError("一般文字受控模式遇到保留的媒體提示文字，無法確認為普通文字；整批停止且未發送。")
            if any(not message.text.strip() for message in to_send):
                raise ValueError("新文字內容為空白，整批停止且未發送。")
            if not args.allow_normal_text and any(not message.text.startswith(prefix) and not (args.include_images and message.text == "圖片") for message in to_send):
                raise ValueError("新內容不是約定的測試前綴；可能是一般訊息、媒體或系統事件，整批停止且未發送。")
            if any(len(message.text.encode("utf-16-le")) // 2 > 4096 for message in to_send):
                raise ValueError("測試文字太長，整批停止且未發送。")
            if sent_count + len(to_send) > args.max_messages:
                raise ValueError("新增內容超過本輪測試上限，整批停止且未發送。")
            read_seconds = round(time.monotonic() - check_started, 3)
            logging.info("檢查完成；發現新訊息=%s；數量=%s；讀取耗時=%.3f 秒。", bool(batch), len(batch), read_seconds)
            save("watching", has_new=bool(batch), new_count=len(batch), reason=None, copy_failures=0, read_seconds=read_seconds)
            for message in batch:
                if message.sender in ignored_senders:
                    if progress:
                        progress.skip_sender(message)
                    skipped_sender_count += 1
                    logging.info('略過指定發送者；sender=%s；LINE 時間=%s %s；未擷取／未發送。',
                                 message.sender, message.day, message.clock)
                    save('watching', skipped_sender_count=skipped_sender_count,
                         acknowledged_count=progress.state['acknowledged_count'] if progress else None)
                    continue
                processing_started = time.monotonic()
                logging.info("偵測到測試內容；sender=%s；LINE 時間=%s %s；本文=%s", message.sender, message.day, message.clock, message.text)
                save("preparing", pending=asdict(message), send_attempt_started=False,
                     failed_message=None, failed_message_kind=None)
                if message.text == "圖片" and args.include_images:
                    save("capturing_image", pending=asdict(message), pending_kind="image")
                    capture_started = time.monotonic()
                    image_report = run_image_capture(logs / f"image-capture-{stamp}-{sent_count + 1}.json",
                                                     group, current, senders, skip_recall_notices=skip_recalls)
                    capture_seconds = round(time.monotonic() - capture_started, 3)
                    image_path = Path(image_report["sample_path"]).resolve()
                    if image_path.parent != (ROOT / "work" / "line-images").resolve():
                        raise ValueError("擷取結果不在本專案的圖片暫存資料夾。")
                    png = image_path.read_bytes()
                    if hashlib.sha256(png).hexdigest() != image_report["sha256"]:
                        raise ValueError("擷取圖片在發送前已改變，停止。")
                    if time.monotonic() >= deadline:
                        raise ValueError("圖片擷取完成時已超過本輪測試期限，未發送。")
                    save("sending", pending=asdict(message), pending_kind="image", image_sha256=image_report["sha256"])
                    logging.info("圖片身分核對成功；%s × %s；%s bytes；擷取核對耗時=%.3f 秒；準備單次發送。",
                                 image_report["width"], image_report["height"], len(png), capture_seconds)
                    save("sending", capture_seconds=capture_seconds)
                    if progress:
                        progress.begin(message, 'image')
                    save('sending', send_attempt_started=True)
                    upload_started = time.monotonic()
                    result = send_image(token, chat_id, png)
                else:
                    if progress:
                        progress.begin(message, 'text')
                    save('sending', send_attempt_started=True)
                    upload_started = time.monotonic()
                    result = send_text(token, chat_id, message.text)
                upload_seconds = round(time.monotonic() - upload_started, 3)
                processing_seconds = round(time.monotonic() - processing_started, 3)
                trimmed_spaces = result.get("_line_stock_trimmed_trailing_spaces", 0)
                if trimmed_spaces:
                    logging.warning("Telegram 回應去掉原文尾端 %s 個 ASCII 空白；正文及內部換行完全相符，視為成功，不重送。", trimmed_spaces)
                sent_count += 1
                if progress:
                    progress.confirm(result['message_id'])
                logging.info("成功轉傳；第 %s 則；Telegram message_id=%s；上傳耗時=%.3f 秒；偵測後處理耗時=%.3f 秒。",
                             sent_count, result["message_id"], upload_seconds, processing_seconds)
                save("watching", sent_count=sent_count, pending=None, pending_kind=None,
                     send_attempt_started=False, last_message_id=result["message_id"],
                     upload_seconds=upload_seconds, processing_seconds=processing_seconds,
                     last_trimmed_trailing_spaces=trimmed_spaces,
                     acknowledged_count=progress.state['acknowledged_count'] if progress else None)
            previous = current
            if sent_count == args.max_messages:
                save("completed", reason="message_limit")
                logging.info("已完成 %s 則測試，自動停止。", sent_count)
                return 0
        save("completed", reason="time_limit")
        logging.info("到達測試期限，已停止；本輪成功轉傳 %s 則。", sent_count)
        return 0
    except KeyboardInterrupt:
        if owns_lock:
            preserve_failure()
            save("stopped", reason="keyboard_interrupt")
        logging.info("使用者停止；若有 pending，請先查看手機，不自動重送。")
        return 130
    except (ValueError, TelegramError, RuntimeError, TimeoutError) as exc:
        if owns_lock:
            preserve_failure()
            save("error", error=str(exc), reason=("unsupported_recall" if isinstance(exc, UnsupportedRecallError)
                                                  else "operation_failed"))
        logging.error("%s；已停止，不自動重試。", exc)
        return 1
    except Exception as exc:
        error = f"未預期錯誤（{type(exc).__name__}），請查看設定與本機 log。"
        if owns_lock:
            preserve_failure()
            save("error", error=error)
        logging.error("%s；已停止，不自動重試。", error)
        return 1
    finally:
        if lock is not None:
            lock.close()
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
