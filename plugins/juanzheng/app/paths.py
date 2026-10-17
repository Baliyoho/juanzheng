"""卷證的程式與執行期資料各在哪裡——在 NAA_TR repo 裡跑、與裝成 Claude Code 外掛跑，共用這一份判斷。

- **repo 模式**：這個資料夾在 NAA_TR 的 `apps/research_shell/`（上兩層有 `AGENTS.md` 與 `tools/evidence_lookup.py`）。
  執行期資料照舊放 gitignored 的 `data/research_shell/`，轉寫頁的原圖可以退回 repo 的 `sample/` 找，
  人工核對與證據面板 mod 共用 `data/evidence/verdicts.jsonl`。
- **外掛模式**（2026-10-17 起，`tools/build_juanzheng_plugin.py` 打包）：程式被複製到使用者資料夾的 `app/`，
  沒有 repo 可依賴。執行期資料放作業系統慣用的使用者資料夾（見 `user_data_dir()`），`evidence_lookup.py`
  由打包腳本複製一份放在本資料夾。

環境變數 `JUANZHENG_DATA` 可以把執行期資料改到別處（兩種模式都有效；測試、開發用）。
本檔只用標準函式庫：外掛的啟動器在還沒裝好相依套件之前就要 import 它。
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
_UP = HERE.parents[1] if len(HERE.parents) > 1 else HERE
REPO = _UP if (_UP / 'AGENTS.md').is_file() and (_UP / 'tools' / 'evidence_lookup.py').is_file() else None
APP_NAME = 'Juanzheng'


def user_data_dir():
    """作業系統慣用的「每個使用者一份」的資料夾。"""
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or pathlib.Path.home() / 'AppData' / 'Local'
        return pathlib.Path(base) / APP_NAME
    if sys.platform == 'darwin':
        return pathlib.Path.home() / 'Library' / 'Application Support' / APP_NAME
    base = os.environ.get('XDG_DATA_HOME') or pathlib.Path.home() / '.local' / 'share'
    return pathlib.Path(base) / APP_NAME.lower()


if os.environ.get('JUANZHENG_DATA'):
    DATA = pathlib.Path(os.environ['JUANZHENG_DATA']).expanduser().resolve()
elif REPO:
    DATA = REPO / 'data' / 'research_shell'
else:
    DATA = user_data_dir()

# 轉寫頁人工核對的紀錄：repo 模式與證據面板 mod 共用一份
VERDICTS = REPO / 'data' / 'evidence' / 'verdicts.jsonl' if REPO and not os.environ.get('JUANZHENG_DATA') \
    else DATA / 'verdicts.jsonl'
# 改版前的多卷轉寫總表（搬舊題目、離線測試用）；外掛模式沒有
PAGES_JSONL = REPO / 'data' / 'delivery' / 'pages.jsonl' if REPO else None
# 轉寫頁在 source 裡找不到原圖時，最後再找的地方
IMAGE_FALLBACKS = (REPO / 'sample', REPO / 'sample' / '第一批測試') if REPO else ()


def add_evidence_lookup_to_path():
    """`evidence_lookup.py` 的正本在 repo 的 `tools/`；外掛裡是打包時複製到本資料夾的那份。"""
    for p in (HERE, (REPO / 'tools') if REPO else None):       # 後插的排前面：repo 的正本優先
        if p and str(p) not in sys.path:
            sys.path.insert(0, str(p))
