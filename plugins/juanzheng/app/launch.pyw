"""卷證桌面 app 的進入點：桌面／開始功能表的「卷證」圖示（macOS 是「應用程式」裡的「卷證.app」）執行它
（Windows 用 pythonw，不跳命令列視窗）。

**伺服器與視窗在同一個程式裡**：視窗是 pywebview 開的原生視窗——Windows 的畫面引擎是 WebView2
（Edge 的核心，Windows 11 內建），macOS 是系統的 WKWebView（Safari 的核心）；伺服器在同一個程式的背景執行緒。
- 視窗關掉 → 程式結束（還有題目在研究時，背景等它存好才結束）。
- 已在跑時再點圖示 → 把現有視窗叫到前面，不開第二份。
- 工作列圖示由我們設定（juanzheng.ico 多尺寸），並用固定的 AppUserModelID 讓「釘選到工作列」
  釘到的是捷徑本身——點釘選的圖示會走這支程式、先啟動伺服器。

為什麼不用瀏覽器的 --app 視窗（2026-10-08 第一版的做法）：Edge 對沒安裝的 app 視窗，工作列圖示一律
取網頁 16px 小圖示放大，怎麼畫都糊；釘選到工作列後點的是 Edge，不會先啟動伺服器。
pywebview 或 WebView2 不能用時，才退回那個做法（伺服器靠 /api/presence 偵測視窗全關後結束）。

建立捷徑：.venv/Scripts/python -X utf8 apps/research_shell/launch.pyw --install
（Windows：桌面＋開始功能表；macOS：「應用程式」/Applications/卷證.app，寫不進去才放 ~/Applications。
外掛模式由啟動器 `juanzheng.py shortcut` 代跑。）
出錯時跳對話框；紀錄在執行期資料夾（paths.py）的 server.log。macOS 的部分 2026-10-18 在 Apple 晶片實測過。
"""
import ctypes
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402

PORT = int(os.environ.get('JUANZHENG_PORT') or 8790)
URL = f'http://127.0.0.1:{PORT}/'
DATA = paths.DATA
LOG = DATA / 'server.log'
SETTINGS = DATA / 'settings.json'
ICON = HERE / 'juanzheng.ico'
AUMID = 'NAA.Juanzheng'          # 工作列用來辨識「這是卷證」的 ID；捷徑上也要設同一個


# ---------------------------------------------------------------- 小工具

def message(text, error=True):
    try:
        if sys.platform == 'darwin':
            esc = text.replace('\\', '\\\\').replace('"', '\\"')
            subprocess.run(['osascript', '-e', f'display dialog "{esc}" with title "卷證" buttons {{"好"}} '
                            f'default button 1 with icon {"stop" if error else "note"}'], timeout=600)
        else:
            ctypes.windll.user32.MessageBoxW(0, text, '卷證', 0x10 if error else 0x40)
    except Exception:
        print(text, file=sys.stderr)


def log_to_file():
    """pythonw 沒有主控台，print 與錯誤訊息都導到紀錄檔，出事才查得到。"""
    DATA.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 1_000_000:
        LOG.replace(LOG.with_suffix('.old.log'))
    f = open(LOG, 'a', encoding='utf-8', buffering=1)
    f.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 卷證啟動 ====\n")
    sys.stdout = sys.stderr = f


def state():
    """'free'＝埠沒人用；'ours'＝卷證在跑；'other'＝被別的程式佔了。"""
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(('127.0.0.1', PORT)) != 0:
            return 'free'
    try:
        with urllib.request.urlopen(URL + 'api/status', timeout=15) as r:
            return 'ours' if 'corpus' in json.load(r) else 'other'
    except Exception:
        return 'other'


def focus_existing():
    req = urllib.request.Request(URL + 'api/focus', method='POST', data=b'{}',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r).get('ok', False)
    except Exception:
        return False


# ---------------------------------------------------------------- 伺服器（同一個程式）

def start_server():
    import server
    from http.server import ThreadingHTTPServer
    app = server.App()
    server.Handler.app = app
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f'{server.now_str()} 卷證：{URL}', flush=True)
    return server, app, srv


# ---------------------------------------------------------------- 原生視窗

class Api:
    """給網頁呼叫的 Python 方法（window.pywebview.api.*）。底線開頭的屬性不會暴露給網頁。"""

    def __init__(self, app):
        self._app, self._win = app, None

    def pick_folder(self, start=''):
        """Windows 的「選擇資料夾」對話框（對話框裡可以按「新增資料夾」；pywebview 不能自訂標題）。取消回 None。"""
        import webview
        kind = getattr(getattr(webview, 'FileDialog', None), 'FOLDER', None) or webview.FOLDER_DIALOG
        start = start if start and pathlib.Path(start).is_dir() else str(pathlib.Path.home())
        r = self._win.create_file_dialog(kind, directory=start)
        if not r:
            return None
        return r if isinstance(r, str) else r[0]

    def export(self, topic_id):
        """匯出筆記：跳「另存新檔」對話框（WebView2 內建的下載在 pywebview 預設是關的）。"""
        import webview
        t = self._app.store.get(topic_id)
        name = ''.join(c if c not in '\\/:*?"<>|' else '_' for c in t['title'])[:40].strip() + '.md'
        kind = getattr(getattr(webview, 'FileDialog', None), 'SAVE', None) or webview.SAVE_DIALOG
        r = self._win.create_file_dialog(kind, save_filename=name, file_types=('Markdown (*.md)',))
        if not r:
            return None
        path = r if isinstance(r, str) else r[0]
        pathlib.Path(path).write_text(self._app.export(topic_id), encoding='utf-8')
        return path


def run_window(app=None):
    import webview
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    api = Api(app) if app else None
    win = webview.create_window('卷證', URL, js_api=api, width=1440, height=900, min_size=(980, 640),
                                background_color='#F5F3EC', text_select=True)
    if api:
        api._win = win

        def focus():
            try:
                win.restore()
                win.show()
                win.on_top = True       # 暫時置頂再取消：Windows 不讓背景程式直接搶前景
                win.on_top = False
            except Exception as ex:
                print(f'叫出視窗失敗：{ex}', flush=True)
        app.on_focus = focus
    if os.name == 'nt':
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(DATA / 'webview'), icon=str(ICON))
    else:                                       # macOS：pywebview 自動用 Cocoa／WKWebView
        webview.start(private_mode=False, storage_path=str(DATA / 'webview'))


def fallback_browser(app, srv, server):
    """pywebview／WebView2 不能用：退回瀏覽器 app 視窗，伺服器靠 presence 偵測視窗全關後結束。"""
    try:
        choice = json.loads(SETTINGS.read_text(encoding='utf-8')).get('browser', 'auto')
    except (OSError, ValueError):
        choice = 'auto'
    if sys.platform == 'darwin':
        apps = [pathlib.Path(d) for d in ('/Applications', pathlib.Path.home() / 'Applications')]
        edge = [d / 'Microsoft Edge.app/Contents/MacOS/Microsoft Edge' for d in apps]
        chrome = [d / 'Google Chrome.app/Contents/MacOS/Google Chrome' for d in apps]
    else:
        pf, pf86, local = (os.environ.get(k, '') for k in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'))
        edge = [pathlib.Path(p) / 'Microsoft/Edge/Application/msedge.exe' for p in (pf86, pf) if p]
        chrome = [pathlib.Path(p) / 'Google/Chrome/Application/chrome.exe' for p in (pf, pf86, local) if p]
    exe = next((str(e) for e in {'edge': edge, 'chrome': chrome}.get(choice, edge + chrome) if e.is_file()),
               shutil.which('msedge') or shutil.which('chrome'))
    if exe and choice != 'default':
        subprocess.Popen([exe, f'--app={URL}', '--window-size=1440,900'], close_fds=True)
    else:
        webbrowser.open(URL)
    app.watch(srv, idle=20, startup=120)      # 會阻塞到伺服器結束


# ---------------------------------------------------------------- 捷徑

def set_shortcut_appid(lnk, appid):
    """在 .lnk 上寫 System.AppUserModel.ID，讓執行中的視窗與捷徑在工作列上是同一個東西
    （釘選才會釘到捷徑、而不是 pythonw.exe）。直接用 ctypes 呼叫 IPropertyStore，免裝 pywin32。"""
    from ctypes import POINTER, WINFUNCTYPE, byref, c_ulong, c_ushort, c_void_p, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [('raw', ctypes.c_ubyte * 16)]

    def guid(s):
        return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [('fmtid', GUID), ('pid', ctypes.c_ulong)]

    class PROPVARIANT(ctypes.Structure):          # x64 上 24 bytes：vt＋保留 6 bytes＋16 bytes 聯合
        _fields_ = [('vt', c_ushort), ('r1', c_ushort), ('r2', c_ushort), ('r3', c_ushort),
                    ('p', c_void_p), ('pad', c_void_p)]

    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
    ole32.CoInitialize(None)
    iid = guid('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')                     # IID_IPropertyStore
    ps = c_void_p()
    shell32.SHGetPropertyStoreFromParsingName.restype = ctypes.HRESULT
    shell32.SHGetPropertyStoreFromParsingName(c_wchar_p(str(lnk)), None, 2, byref(iid), byref(ps))  # GPS_READWRITE
    vtbl = ctypes.cast(ps, POINTER(POINTER(c_void_p))).contents
    set_value = WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(vtbl[6])
    commit = WINFUNCTYPE(ctypes.HRESULT, c_void_p)(vtbl[7])
    release = WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])
    key = PROPERTYKEY(guid('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3'), 5)     # PKEY_AppUserModel_ID
    text = ctypes.create_unicode_buffer(appid)
    pv = PROPVARIANT(vt=31, p=ctypes.cast(text, c_void_p))                  # VT_LPWSTR
    try:
        set_value(ps, byref(key), byref(pv))
        commit(ps)
    finally:
        release(ps)


MAC_BUNDLE_ID = 'io.github.baliyoho.juanzheng'


def install_mac():
    """在「應用程式」建「卷證.app」：一個只有啟動腳本的最小 bundle，用這個 Python 執行本檔。
    放 /Applications（Finder 側邊欄的「應用程式」；2026-10-18 使用者要求，原本放 ~/Applications 使用者找不到），
    寫不進去（非管理員帳號）才退回 ~/Applications；另一處若有我們建的舊「卷證.app」就移除，免得 Launchpad 出現兩個。
    圖示由 static/icon-512.png 轉成 .icns（Pillow 會寫 ICNS）。未簽章；是本機自己建的檔，沒有隔離屬性，Gatekeeper 不擋。"""
    system, user = pathlib.Path('/Applications'), pathlib.Path.home() / 'Applications'
    app = (system if os.access(system, os.W_OK) else user) / '卷證.app'
    for old in (system / '卷證.app', user / '卷證.app'):
        try:
            if old != app and MAC_BUNDLE_ID in (old / 'Contents' / 'Info.plist').read_text(encoding='utf-8'):
                shutil.rmtree(old)
        except OSError:                          # 沒有舊的，或刪不掉（別的帳號建在 /Applications 的）就算了
            pass
    macos, res = app / 'Contents' / 'MacOS', app / 'Contents' / 'Resources'
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    (app / 'Contents' / 'Info.plist').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>卷證</string>
<key>CFBundleDisplayName</key><string>卷證</string>
<key>CFBundleIdentifier</key><string>''' + MAC_BUNDLE_ID + '''</string>
<key>CFBundleExecutable</key><string>juanzheng</string>
<key>CFBundleIconFile</key><string>juanzheng</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
''', encoding='utf-8')
    exe = macos / 'juanzheng'
    script = HERE / 'launch.pyw'
    # Apple 晶片上 LaunchServices 用 Rosetta 跑腳本型 app，universal 的 python.org Python 會跟著變 x86_64，
    # 載不了 pip 裝的 arm64 套件（2026-10-18 Mac 實測：從 Finder 開就 ImportError，終端機開正常）
    exe.write_text('#!/bin/sh\n'
                   'if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then\n'
                   f'  exec /usr/bin/arch -arm64 "{sys.executable}" "{script}" "$@"\n'
                   'fi\n'
                   f'exec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
    exe.chmod(0o755)
    try:
        from PIL import Image
        Image.open(HERE / 'static' / 'icon-512.png').save(res / 'juanzheng.icns')
    except Exception as ex:                      # 沒有圖示也能用
        print(f'圖示沒有轉成功：{ex!r}')
    subprocess.run(['touch', str(app)])          # 讓 Finder 重新讀圖示
    print(app)
    return 0


def install():
    """Windows：桌面與開始功能表各建一個「卷證」捷徑。開始功能表那個也讓卷證搜得到，並讓釘選認得它。"""
    if sys.platform == 'darwin':
        return install_mac()
    pythonw = pathlib.Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file() and paths.REPO:
        pythonw = paths.REPO / '.venv' / 'Scripts' / 'pythonw.exe'
    made = []
    for folder in ('Desktop', 'Programs'):
        # 中文 Windows 的 PowerShell 預設用 Big5 輸出；先切成 UTF-8，回傳的路徑才讀得回來
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              f"$d=[Environment]::GetFolderPath('{folder}');$p=Join-Path $d '卷證.lnk';"
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
              f"$s.TargetPath='{pythonw}';$s.Arguments='\"{HERE / 'launch.pyw'}\"';"
              f"$s.WorkingDirectory='{paths.REPO or HERE}';$s.IconLocation='{ICON},0';"
              "$s.Description='卷證：案卷問證（本機文獻研究）';$s.Save();$p")
        r = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        lnk = (r.stdout or '').strip()
        if r.returncode or not lnk:
            print((r.stderr or '').strip())
            return 1
        set_shortcut_appid(lnk, AUMID)
        made.append(lnk)
    print('\n'.join(made))
    return 0


# ---------------------------------------------------------------- 進入點

def main():
    if '--install' in sys.argv:
        return install()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:
        pass
    s = state()
    if s == 'other':
        message(f'埠 {PORT} 被別的程式佔用了，卷證無法啟動。\n\n請關掉那個程式後再試一次。')
        return 1
    log_to_file()
    if s == 'ours':
        if focus_existing():
            return 0
        run_window()              # 開發用的伺服器在跑（沒有視窗可叫）：只開視窗，不管伺服器
        return 0
    try:
        server, app, srv = start_server()
    except Exception as ex:
        print(f'伺服器啟動失敗：{ex!r}', flush=True)
        message(f'卷證伺服器沒有啟動成功。\n\n紀錄在：\n{LOG}')
        return 1
    try:
        run_window(app)
    except Exception as ex:
        print(f'原生視窗開不起來，改用瀏覽器：{ex!r}', flush=True)
        fallback_browser(app, srv, server)
        return 0
    if app.busy():
        print(f'{server.now_str()} 視窗關了，但還有題目在研究；存好後結束。', flush=True)
        while app.busy():
            time.sleep(1)
    print(f'{server.now_str()} 卷證結束。', flush=True)
    srv.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
