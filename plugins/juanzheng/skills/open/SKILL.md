---
name: open
description: 開啟「卷證」桌面視窗（本機文獻研究：Claude 只讀研究專案 source/ 裡的史料作答，逐條核對「」引文，右欄並排原件），或安裝／更新它、建立桌面捷徑。Use when the user says 打開卷證、開卷證、啟動卷證、卷證捷徑、安裝卷證、open juanzheng, or asks to set up the 卷證 app.
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/bin/juanzheng.py" *)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/bin/juanzheng.py" *)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/bin/juanzheng.py" *)
---

# 開啟卷證

卷證是獨立的桌面視窗（伺服器只聽 127.0.0.1）。啟動器 `${CLAUDE_PLUGIN_ROOT}/bin/juanzheng.py` 只用標準函式庫，
第一次執行會自己建虛擬環境、裝相依套件、把程式複製到使用者資料夾，之後直接開。

## 步驟

1. 選 Python 指令：macOS／Linux 用 `python3`；Windows 用 `python`（找不到或跳出 Microsoft Store 時改用 `py -3`）。
2. 執行（**timeout 設 600000**：第一次安裝要下載數百 MB，包括 Claude Agent SDK 內建的 Claude Code CLI）：

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/bin/juanzheng.py" open
   ```

3. 照輸出回報一兩句：開了沒有、網址、紀錄檔位置。不要貼整段 pip 輸出。

使用者要的是別的動作時，把 `open` 換成：

| 使用者要 | 子指令 |
|---|---|
| 桌面捷徑（Windows：桌面＋開始功能表；macOS：「應用程式」的 `卷證.app`） | `shortcut` |
| 只安裝或更新、不開視窗 | `setup`（重建虛擬環境：`setup --force`） |
| 裝在哪、是不是最新、是否在跑 | `status` |

## 出問題時

- **需要 Python 3.10 以上**：啟動器找不到會印出安裝方式，照實轉告使用者；不要自己去下載安裝程式。
- **問答需要 Claude 訂閱登入**（Pro／Max）。卷證每題開跑前檢查，不是訂閱登入就拒絕作答，避免改走 API 計費。
  使用者沒登入時請他在終端機執行 `claude` 再 `/login`。只裝 Claude 桌面版、沒有 `claude` 指令時（桌面版的登入卷證看不到），
  請他執行卷證虛擬環境裡 `site-packages/claude_agent_sdk/_bundled/claude auth login --claudeai`（虛擬環境在 `status` 印出的資料夾）。
- 埠 8790 被別的程式佔用時卷證不會啟動；可設環境變數 `JUANZHENG_PORT` 換埠。
- 視窗沒出現：讀 `status` 印出的資料夾裡的 `server.log`，把錯誤講給使用者聽。
- macOS 只在 Apple 晶片實測過（2026-10-18）；遇到 Mac 專屬錯誤，照實回報，不要猜測已修好。
