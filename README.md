# 卷證（案卷問證）— Claude Code 外掛

在自己電腦上研究一批文獻：Claude 只讀你放進研究專案的史料作答，程式把回答裡每段「」引文逐條比對原文，
右欄並排原件掃描。macOS／Windows 通用。

- **只走你的 Claude 訂閱（Pro／Max），不走 API 計費**：每題開跑前確認是訂閱登入，並清掉 `ANTHROPIC_API_KEY` 等環境變數。
- **只聽 127.0.0.1，不對外**。史料、研究題目都留在你的電腦。
- 模型只拿得到三個唯讀工具（搜尋、讀頁、目錄），讀不到研究專案以外的任何檔案。

> 本 repo 只有程式。它是從私人研究 repo 打包產生的（`tools/build_juanzheng_plugin.py`），
> 請不要直接在這裡改程式——改了下次打包會被蓋掉。

## 安裝

需要：[Claude Code](https://claude.com/claude-code)（已用訂閱帳號 `/login`）、Python 3.10 以上。

在 Claude Code 裡：

```text
/plugin marketplace add Baliyoho/juanzheng
/plugin install juanzheng@juanzheng
```

然後說「打開卷證」（或 `/juanzheng:open`）。第一次會花幾分鐘安裝：啟動器在你的使用者資料夾建虛擬環境、
裝相依套件（含 Claude Agent SDK 內建的 Claude Code CLI，數百 MB），之後就是秒開。
要桌面捷徑就說「建卷證捷徑」（`shortcut`）：Windows 放桌面＋開始功能表，macOS 放 `~/Applications/卷證.app`。

不經 Claude Code 也能直接用啟動器：

```bash
python3 plugins/juanzheng/bin/juanzheng.py open
```

（Windows 把 `python3` 換成 `python`。）

## 用法

1. 在卷證視窗左上角新建或開啟一個**研究專案**＝一個資料夾，裡面三個子資料夾：
   - `source/`：研究材料。PDF（取內嵌文字層）、txt／md／epub，或「文件資料夾」（`original.*` 原件＋`pages.jsonl` 轉寫）。
     `_` 或 `.` 開頭的檔案不會被當成史料。
   - `context/`：研究指示與背景筆記（.md／.txt），每題開跑前放進給 Claude 的指示。筆記不能當出處。
   - `session/`：研究題目，一題一個 JSON（問答、研究足跡、引文核對與你的判斷）。
2. 選範圍、模型（Opus／Sonnet）與 effort，提問。回答裡每段「」都會標示：逐字吻合／僅異體字不同／相近／找不到。
3. 點【出處】在右欄看原件與原文，標記「相符／有誤／看不清」，可匯出 Markdown 筆記。

在 Claude Code 對話裡也能直接查同一個專案（`research` skill）：「用卷證查監察院對工業用地編定的糾正」——
Claude 會用命令列搜尋、讀頁，作答前逐條核對引文。

🔴 **掃描 PDF 沒有文字層就搜不到、不能引用**：要配上轉寫（文件資料夾的 `pages.jsonl`）。
🔴 **轉寫是機器產生的**：「逐字吻合」只證明字在轉寫裡，不證明讀對了；人名、日期、數字請看原件。

## 資料放在哪

| 東西 | 位置 |
|---|---|
| 程式與虛擬環境、最近專案清單、快取、紀錄檔 `server.log` | Windows `%LOCALAPPDATA%\Juanzheng\`；macOS `~/Library/Application Support/Juanzheng/` |
| 史料、研究題目 | 你自己選的研究專案資料夾（可放任何地方、整包搬走） |

移除：在 Claude Code `/plugin uninstall juanzheng@juanzheng`，再刪掉上表第一列的資料夾與捷徑。研究專案資料夾不會被動到。

## 限制

- **macOS 尚未在實機驗證**（2026-10-17 在 Windows 11 寫成並實測；Mac 的視窗、捷徑、對話框是照文件寫的）。
- 沒做 OCR：掃描檔要自備轉寫。不支援 docx、xlsx。
- 一次開一個專案、同一時間只跑一題（訂閱額度是一個人的）。
- 搜尋是逐頁子字串比對（含異體字、全半形正規化），不是向量語意檢索。

## 授權

[MIT](LICENSE)。
