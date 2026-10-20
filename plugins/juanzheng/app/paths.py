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
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
_UP = HERE.parents[1] if len(HERE.parents) > 1 else HERE
REPO = _UP if (_UP / 'AGENTS.md').is_file() and (_UP / 'tools' / 'evidence_lookup.py').is_file() else None
APP_NAME = 'Juanzheng'


def user_data_dir():
    """「每個使用者一份」的資料夾。macOS／Linux 用作業系統慣用的位置。

    Windows 刻意**不用** `%LOCALAPPDATA%`（2026-10-20 起；1.0.3 以前是 `%LOCALAPPDATA%\\Juanzheng`）：
    Claude 桌面版是 MSIX 封裝，從它底下跑的子行程（Claude Code → 啟動器 → pip）寫進 AppData 的新檔
    會被系統重導到 `%LOCALAPPDATA%\\Packages\\Claude_…\\LocalCache\\`，封裝外（檔案總管、一般終端機）
    看不到，Claude 桌面版解除安裝時還會一起刪掉。使用者資料夾根目錄不受重導，封裝內外看到的是同一份。
    """
    if os.name == 'nt':
        return pathlib.Path.home() / ('.' + APP_NAME.lower())
    if sys.platform == 'darwin':
        return pathlib.Path.home() / 'Library' / 'Application Support' / APP_NAME
    base = os.environ.get('XDG_DATA_HOME') or pathlib.Path.home() / '.local' / 'share'
    return pathlib.Path(base) / APP_NAME.lower()


def legacy_data_dirs():
    """1.0.3 以前 Windows 的執行期資料夾：真正的 `%LOCALAPPDATA%\\Juanzheng`，以及被 MSIX 重導過去的那些。
    在封裝內看第一個，看到的是兩者合併後的樣子；在封裝外只能直接去 `Packages\\*\\LocalCache` 找。"""
    if os.name != 'nt' or not os.environ.get('LOCALAPPDATA'):
        return []
    local = pathlib.Path(os.environ['LOCALAPPDATA'])
    return [local / APP_NAME, *sorted(local.glob(f'Packages/*/LocalCache/Local/{APP_NAME}'))]


def redirected(p):
    """`p` 實際落在別處（MSIX 重導、junction）時回傳實際位置，否則 None。
    檔案總管與一般程式只認實際位置，所以捷徑、要給封裝外用的路徑都要寫這個。"""
    real = os.path.realpath(p)
    return pathlib.Path(real) if os.path.normcase(real) != os.path.normcase(_long_name(os.path.abspath(p))) else None


def _long_name(p):
    """把 Windows 8.3 短檔名（`MESHRO~1`）展開；realpath 會展開，不先展開會把它誤認成重導。"""
    if os.name != 'nt':
        return p
    import ctypes
    head, rest = p, ''
    while head and not os.path.exists(head):                 # 還不存在的部分原樣接回去
        head, tail = os.path.split(head)
        if not tail:
            return p
        rest = os.path.join(tail, rest) if rest else tail
    buf = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetLongPathNameW(head, buf, len(buf)):
        return p
    return os.path.join(buf.value, rest) if rest else buf.value


def app_version(here=None):
    """畫面左下角顯示的版本。外掛版：打包時寫進 app/ 的 `VERSION`（2026-10-20 起）。
    repo 開發版：沒有 VERSION，讀 `plugin/.claude-plugin/plugin.json` 的版號並標開發版、附 git 短碼——
    兩者用同一個埠、長得一樣，不標出來分不清現在開的是哪一個。"""
    here = pathlib.Path(here or HERE)
    f = here / 'VERSION'
    if f.is_file():
        return {'version': f.read_text(encoding='utf-8').strip() or None, 'dev': False, 'commit': None}
    try:
        v = json.loads((here / 'plugin' / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        v = None
    commit = None
    try:
        r = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(here), capture_output=True, text=True,
                           timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode == 0:
            commit = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {'version': v, 'dev': True, 'commit': commit}

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
