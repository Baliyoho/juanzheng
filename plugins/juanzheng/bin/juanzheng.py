"""卷證外掛的啟動器（macOS／Windows 通用，只用標準函式庫，Python 3.10 以上）。

    python3 juanzheng.py open        # 第一次會先安裝：建虛擬環境、裝相依套件、複製程式；然後開卷證視窗
    python3 juanzheng.py setup       # 只安裝／更新，不開視窗
    python3 juanzheng.py shortcut    # 建捷徑：Windows 桌面＋開始功能表；macOS ~/Applications/卷證.app
    python3 juanzheng.py status      # 裝在哪裡、版本、是否在跑
    python3 juanzheng.py search 關鍵詞… | read 頁碼 | catalog | context | verify 頁碼 "引文" | projects
                                     # 命令列查詢研究專案（不呼叫模型），參數見 app/cli.py

為什麼要把程式複製到使用者資料夾再跑：外掛每次更新都會換一個安裝路徑，桌面捷徑若指著外掛資料夾，
更新後就斷了。所以程式與虛擬環境固定放在 `<使用者資料夾>/Juanzheng/`（位置見 app/paths.py），
捷徑指那裡；外掛內容變了（雜湊不同）才重新複製，相依套件清單變了才重新 pip install。
卷證開著的時候不換程式，下次再換。
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
SRC_APP = PLUGIN / 'app'
REQUIREMENTS = PLUGIN / 'requirements.txt'
sys.path.insert(0, str(SRC_APP))
import paths  # noqa: E402  外掛 app/ 裡的那份；只用標準函式庫

DATA = paths.DATA
APP = DATA / 'app'
VENV = DATA / 'venv'
STATE = DATA / 'install.json'
PORT = int(os.environ.get('JUANZHENG_PORT') or 8790)
URL = f'http://127.0.0.1:{PORT}/'
MIN_PY = (3, 10)
QUERY_CMDS = ('search', 'read', 'catalog', 'context', 'verify', 'projects')


def say(*a):
    print(*a, file=sys.stderr, flush=True)


def venv_python(gui=False):
    if os.name == 'nt':
        return VENV / 'Scripts' / ('pythonw.exe' if gui else 'python.exe')
    return VENV / 'bin' / 'python'


def digest(root, files=None):
    h = hashlib.sha256()
    for p in sorted(files or (q for q in root.rglob('*') if q.is_file() and '__pycache__' not in q.parts)):
        h.update(p.relative_to(root).as_posix().encode('utf-8') + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()[:16]


def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def py_version(exe):
    try:
        r = subprocess.run([str(exe), '-c', 'import sys;print(sys.version_info[0],sys.version_info[1])'],
                           capture_output=True, text=True, timeout=20)
        return tuple(int(x) for x in r.stdout.split()[:2]) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def find_python():
    """建虛擬環境用的 Python：執行本檔的那個夠新就用它，否則找系統裡較新的（macOS 內建的 python3 常是 3.9）。"""
    if sys.version_info[:2] >= MIN_PY:
        return sys.executable
    cands = [shutil.which(n) for n in ('python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python')]
    cands += ['/opt/homebrew/bin/python3', '/usr/local/bin/python3']
    cands += sorted((str(p) for p in pathlib.Path('/Library/Frameworks/Python.framework/Versions').glob('3.*/bin/python3')),
                    reverse=True)
    for c in cands:
        if c and pathlib.Path(c).is_file() and (py_version(c) or (0, 0)) >= MIN_PY:
            return c
    raise SystemExit('卷證需要 Python 3.10 以上，這台電腦找不到。\n'
                     '  macOS：到 https://www.python.org/downloads/ 安裝，或 `brew install python@3.12`\n'
                     '  Windows：到 https://www.python.org/downloads/ 安裝（勾選 Add python.exe to PATH）')


def running():
    try:
        with urllib.request.urlopen(URL + 'api/status', timeout=3) as r:
            return 'corpus' in json.load(r)
    except Exception:
        return False


def setup(force=False):
    """需要時才做：建 venv、pip install、複製 app/。回傳是否有變動。"""
    st, changed = load_state(), False
    DATA.mkdir(parents=True, exist_ok=True)
    req_hash, app_hash = digest(PLUGIN, [REQUIREMENTS]), digest(SRC_APP)
    if force or not venv_python().is_file():
        base = find_python()
        say(f'建立虛擬環境：{VENV}（Python：{base}）')
        if VENV.exists():
            shutil.rmtree(VENV)
        subprocess.run([base, '-m', 'venv', str(VENV)], check=True)
        st.pop('requirements', None)
        changed = True
    if st.get('requirements') != req_hash:
        say('安裝相依套件（第一次約需數分鐘，含 Claude Agent SDK 內建的 Claude Code CLI）…')
        py = str(venv_python())
        subprocess.run([py, '-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--upgrade', 'pip'],
                       check=False)
        subprocess.run([py, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(REQUIREMENTS)],
                       check=True)
        st['requirements'] = req_hash
        changed = True
    if st.get('app') != app_hash or not (APP / 'launch.pyw').is_file():
        if running():
            say('卷證正開著，這次先不更新程式；關掉卷證後再執行一次 setup 或 open 就會更新。')
        else:
            tmp = DATA / 'app.new'
            if tmp.exists():
                shutil.rmtree(tmp)
            shutil.copytree(SRC_APP, tmp, ignore=shutil.ignore_patterns('__pycache__'))
            if APP.exists():
                shutil.rmtree(APP)
            tmp.replace(APP)
            st['app'] = app_hash
            changed = True
    st.update({'version': plugin_version(), 'plugin': str(PLUGIN), 'updated': time.strftime('%Y-%m-%d %H:%M:%S')})
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    return changed


def plugin_version():
    try:
        return json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        return None


def focus():
    req = urllib.request.Request(URL + 'api/focus', method='POST', data=b'{}',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r).get('ok', False)
    except Exception:
        return False


def open_app():
    if running():
        print('卷證已經開著，' + ('已叫到前面。' if focus() else f'但叫不出視窗（可能是開發用的伺服器）：{URL}'))
        return 0
    setup()
    launch = APP / 'launch.pyw'
    kw = {'cwd': str(APP), 'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        kw['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(venv_python(gui=True)), str(launch)], close_fds=True, **kw)
    else:
        subprocess.Popen([str(venv_python()), str(launch)], start_new_session=True, **kw)
    for _ in range(60):                       # 伺服器起來約數秒；第一次開專案可能更久
        if running():
            print(f'卷證已開啟（{URL}）。視窗沒出現的話看紀錄：{DATA / "server.log"}')
            return 0
        time.sleep(0.5)
    print(f'已啟動卷證，但 30 秒內沒有回應。請看紀錄：{DATA / "server.log"}')
    return 1


def main():
    args = sys.argv[1:] or ['open']
    cmd = args[2] if args[0] == '--project' and len(args) > 2 else args[0]
    for s in (sys.stdout, sys.stderr):
        if s and hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    if cmd == 'open':
        return open_app()
    if cmd == 'setup':
        changed = setup(force='--force' in args)
        print(f"卷證{'已安裝／更新' if changed else '已是最新'}：{DATA}")
        return 0
    if cmd == 'shortcut':
        setup()
        return subprocess.run([str(venv_python()), str(APP / 'launch.pyw'), '--install']).returncode
    if cmd == 'status':
        st = load_state()
        print(json.dumps({'data': str(DATA), 'installed': venv_python().is_file() and (APP / 'launch.pyw').is_file(),
                          'installed_version': st.get('version'), 'plugin_version': plugin_version(),
                          'up_to_date': st.get('app') == digest(SRC_APP), 'running': running(), 'url': URL},
                         ensure_ascii=False, indent=1))
        return 0
    if cmd in QUERY_CMDS:
        if not venv_python().is_file() or not (APP / 'cli.py').is_file():
            setup()
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
        return subprocess.run([str(venv_python()), str(APP / 'cli.py'), *args], env=env).returncode
    say(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
