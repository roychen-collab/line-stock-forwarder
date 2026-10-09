# line-stock-forwarder

Windows 官方 LINE 聊天室 → 私人 Telegram 的 Python 自動化原型。
本份副本供程式檢閱，沒有私人設定、聊天紀錄、圖片、成員名單或 Telegram Token。

## 目前功能與限制

- 純 UI Automation 能辨識聊天室與輸入框，但無法直接讀取訊息文字。
- 使用者授權後，改用官方介面的正常選取／複製取得公開聊天文字。
- 已測試文字順序、重複文字、換行、進度保存與限時轉傳。
- 單張圖片使用公開畫面、官方檢視器、OCR 及正常圖片複製；部分實測成功。
- 需要 Windows 解鎖、來源聊天室在前景，背景轉傳尚未支援。
- 特殊符號姓名 OCR、匿名連續圖片、多人圖片與混合圖片批次仍有限制。
- 正式全成員轉傳尚未完成。發送結果不明時停止，不自動重試。
- 不使用 LINE 私人 API、LINE access token、封包、私有資料庫或逆向通訊協定。

## 程式入口

| 檔案 | 用途 |
| --- | --- |
| diagnostic.py | Windows UI Automation 探測 |
| clipboard_probe.py | 正常複製文字、核對來源與焦點 |
| readonly_watch.py | 指定來源的限時唯讀觀察，不發送 |
| chat_text.py | 文字訊息與姓名邊界解析 |
| forward_test.py | 有上限的測試群轉傳 |
| forward_progress.py | 保存進度及待確認發送 |
| image_capture.py / viewer_identity.py | 圖片擷取及標頭核對 |
| telegram_client.py / setup_telegram.py | 官方 Telegram Bot API 與本機配對 |
| status.py | 本機執行狀態 |
| tests/ | 本機模擬測試；不代表所有現場情境通過 |

## 在另一台 Windows 電腦準備環境

1. 從 [Python 官方網站](https://www.python.org/downloads/windows/) 安裝 Python 3.13，勾選 Add Python to PATH。
2. 解壓或下載本專案後，在資料夾空白處按右鍵開啟終端機，執行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

3. 用記事本開啟 config.json，填寫自己的測試群名稱與完整成員顯示名稱。中文採 UTF-8。
4. 先雙擊 inspect-line.bat 做探測。這不會發送訊息，也不代表完整轉傳已可用。
5. 需要 Telegram 測試時，由帳號持有人雙擊 setup-telegram.bat 配對自己的 Bot 與私人接收帳號。
   Token 儲存在 Windows 認證管理員；分享副本的接收 ID 為無效占位值，不可直接發送。
6. 已配對且測試群設定正確後，test-resume-forward.bat 才能做最多十分鐘／四則的受控測試。
   按 Ctrl+C 停止，雙擊 status.bat 看狀態。先用測試群，勿直接監控正式私人群。

執行本機模擬測試：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

遇到焦點、複製未更新、OCR 或保存進度錯誤時，保留本機 logs 並停止排查；
不要刪掉待確認紀錄強行重送。logs、work、config.json、formal_member_roster.json 與 .venv 都不應上傳。
