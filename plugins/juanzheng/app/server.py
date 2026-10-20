"""卷證：本機文獻研究 shell 的伺服器（只聽 127.0.0.1，不對外）。

    .venv/Scripts/python -X utf8 apps/research_shell/server.py [--port 8790]

一次開一個研究專案（工作區，見 workspace.py）：模型只查它的 source/，研究題目存在它的
session/<id>.json（含史料內容——專案資料夾不要放進 Git）。轉寫頁的人工核對另外附加到
`paths.VERDICTS`（repo 模式是 `data/evidence/verdicts.jsonl`，與證據面板 mod 共用同一份紀錄）。
執行期資料（最近專案清單、快取）的位置見 `paths.py`：repo 模式在 `data/research_shell/`，外掛模式在使用者資料夾。
同一時間只跑一個問答：訂閱額度是一個人的，平行開跑只會更快撞到上限。
"""
import argparse
import datetime
import json
import mimetypes
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from corpus import PAGES_JSONL, ROOT, Corpus, ev  # noqa: E402
import paths  # noqa: E402
import render  # noqa: E402
import sources  # noqa: E402
import workspace  # noqa: E402
from agent import EFFORTS, LIMIT_MINUTES, MODELS, Job, Runner  # noqa: E402

STATIC = HERE / 'static'
DATA = paths.DATA
LEGACY_TOPICS = DATA / 'topics'          # 改版前（2026-10-16 以前）研究題目的位置，搬進專案後改名留存
REGISTRY = DATA / 'workspaces.json'
CACHE = DATA / 'cache'                   # PDF 文字層快取
IMG_CACHE_MAX = 48
VERSION = paths.app_version()             # 啟動時讀一次：外掛版號，或開發版＋git 短碼


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def plan_delta(before, after):
    """問答前後的訂閱用量差（百分點）。只是近似：期間同一帳號在別處（Claude Code、claude.ai）的用量也算在內；
    跨過重置時間時差值會變負，那種情況不顯示。"""
    if not (before and after and before.get('ok') and after.get('ok')):
        return None
    prev = {b['key']: b for b in before['bars']}
    out = []
    for b in after['bars']:
        p = prev.get(b['key'])
        if p and p['resets_raw'] == b['resets_raw'] and b['pct'] >= p['pct']:
            out.append({'key': b['key'], 'label': b['label'], 'delta': round(b['pct'] - p['pct'], 1),
                        'after': b['pct']})
    return out or None


class Store:
    """研究題目：一題一個 JSON。寫入先寫暫存檔再改名，當機也不會留下半個檔。"""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def path(self, tid):
        if not re.fullmatch(r't\d{14}[0-9a-f]{4}', tid or ''):
            raise KeyError(tid)
        return self.root / f'{tid}.json'

    def list(self):
        out = []
        for f in self.root.glob('t*.json'):
            t = json.loads(f.read_text(encoding='utf-8'))
            out.append({'id': t['id'], 'title': t['title'], 'updated': t['updated'],
                        'turns': sum(1 for x in t['turns'] if x.get('status') == 'done')})
        return sorted(out, key=lambda t: t['updated'], reverse=True)

    def get(self, tid):
        p = self.path(tid)
        if not p.exists():
            raise KeyError(tid)
        return json.loads(p.read_text(encoding='utf-8'))

    def save(self, topic):
        topic['updated'] = now()
        p = self.path(topic['id'])
        tmp = p.with_suffix('.tmp')
        with self.lock:
            tmp.write_text(json.dumps(topic, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(p)
        return topic

    def create(self, title=''):
        tid = 't' + time.strftime('%Y%m%d%H%M%S') + secrets.token_hex(2)
        return self.save({'id': tid, 'title': title.strip() or '未命名題目', 'created': now(),
                          'session_id': None, 'turns': []})


def use_data_dir(path):
    """開發／測試用的沙箱：最近專案清單、快取、舊題目、人工核對紀錄都改放 path，不碰真的資料。"""
    global DATA, LEGACY_TOPICS, REGISTRY, CACHE
    DATA = pathlib.Path(path).resolve()
    LEGACY_TOPICS, REGISTRY, CACHE = DATA / 'topics', DATA / 'workspaces.json', DATA / 'cache'
    ev.LOG = DATA / 'verdicts.jsonl'


def build_corpus(ws):
    """讀專案 source/ → (Corpus, 掃描報告)。測試會換掉這個函式。"""
    records, report = sources.scan(ws.source, CACHE)
    return Corpus(records), report


class App:
    def __init__(self):
        self.ws = self.corpus = self.store = self.report = None
        self.ws_error = None                           # 上次的專案打不開時，給畫面的說明
        self.registry = workspace.Registry(REGISTRY)
        self.runner = Runner(None)
        self.runner.context = lambda: self.ws.context()[0] if self.ws else ''
        self.jobs, self.active = {}, None
        self.lock = threading.Lock()
        self.img_cache = {}
        self.windows, self.ever_opened = 0, False      # 目前開著的卷證視窗數（/api/presence 連線數）
        self.on_focus = None                           # 桌面 app 模式：再點一次圖示時，把現有視窗叫到前面
        threading.Thread(target=self.runner.check_auth, daemon=True).start()
        last = self.registry.load()['current']
        if last:
            try:
                self.open_workspace(last)
            except Exception as ex:                    # 資料夾被搬走、隨身碟沒插…：讓使用者重選，不要整個起不來
                self.ws_error = f'上次的研究專案打不開：{last}（{ex}）'
                print(self.ws_error, flush=True)

    def busy(self):
        return bool(self.active and not self.jobs[self.active].done)

    # ---------------------------------------------------------------- 研究專案（工作區）
    def need_ws(self):
        if not self.ws:
            raise RuntimeError('還沒選研究專案：請先在左上角開啟或新建一個專案資料夾。')
        return self.ws

    def open_workspace(self, path, init=False):
        """開專案。資料夾還不是專案（缺子資料夾）時，沒有 init 就回 need_init 讓畫面先問使用者。"""
        if self.busy():
            raise RuntimeError('有題目正在研究，等它完成或停止後再切換專案。')
        info = workspace.inspect(path)
        if not info['is_dir'] and not init:
            raise ValueError(f"找不到這個資料夾：{info['path']}")
        if info['missing'] and not init:
            return {'need_init': True, **info}
        ws = workspace.Workspace.init(path) if init else workspace.Workspace(path)
        t = time.time()
        corpus, report = build_corpus(ws)
        warning = workspace.git_warning(ws.root, ROOT) if ROOT else None
        with self.lock:
            self.ws, self.corpus, self.report, self.store = ws, corpus, report, Store(ws.session)
            self.ws_warning = warning
            self.runner.corpus = corpus
            self.img_cache.clear()
            self.ws_error = None
        self.registry.touch(ws.root)
        print(f'{now_str()} 研究專案：{ws.root}（{len(corpus.vols)} 份、{len(corpus.pages)} 頁，'
              f'{time.time() - t:.1f} 秒）', flush=True)
        return {'ok': True}

    def create_workspace(self, parent, name):
        name = (name or '').strip()
        if not name or re.search(r'[\\/:*?"<>|]', name) or name in ('.', '..'):
            raise ValueError('專案名稱不能空白，也不能有 \\ / : * ? " < > | 這些字元')
        if not pathlib.Path(parent).is_dir():
            raise ValueError(f'找不到要放專案的資料夾：{parent}')
        path = pathlib.Path(parent) / name
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError(f'已經有同名的東西了：{path}（要用它的話請改按「開啟專案資料夾」）')
        return self.open_workspace(path, init=True)

    def reload_workspace(self):
        return self.open_workspace(self.need_ws().root)

    def migrate(self, parent, name):
        """把改版前的研究題目搬進一個新專案（workspace.migrate），然後開啟它。"""
        if not workspace.legacy_topics(LEGACY_TOPICS) or not PAGES_JSONL:
            raise ValueError('沒有要搬的舊題目')
        if self.busy():
            raise RuntimeError('有題目正在研究，等它完成或停止後再搬。')
        self.create_workspace(parent, name) if name else self.open_workspace(parent, init=True)
        r = workspace.migrate(LEGACY_TOPICS, self.ws, PAGES_JSONL)
        self.reload_workspace()                        # source 多了總表，要重讀
        print(f'{now_str()} 舊題目 {r["moved"]} 題搬進 {self.ws.root}；原資料夾留在 {r["backup"]}', flush=True)
        return {'ok': True, **r}

    def import_pack(self, path, images=False):
        """素材包轉成文件資料夾放進目前專案的 source（importer.import_pack），然後重讀。來源只讀不改。"""
        import importer
        ws = self.need_ws()
        if self.busy():
            raise RuntimeError('有題目正在研究，等它完成或停止後再匯入。')
        err = importer.check_pack(path)
        if err:
            raise ValueError(err)
        t = time.time()
        r = importer.import_pack(path, ws.source, copy_images=bool(images),
                                 log=lambda s: print(f'{now_str()} {s}', flush=True))
        self.reload_workspace()
        print(f"{now_str()} 素材包匯入 {ws.root}：新建 {r['made']} 卷、已存在 {len(r['kept'])}、"
              f"沒匯入 {len(r['problems'])}（{time.time() - t:.0f} 秒）", flush=True)
        return {'ok': True, **r}

    def reveal(self, sub):
        root = self.need_ws().root
        target = root / sub if sub in workspace.SUBDIRS else root
        if os.name == 'nt':
            os.startfile(str(target))                  # 只有 Windows 有
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(target)])
        else:
            subprocess.Popen(['xdg-open', str(target)])
        return {'ok': True, 'path': str(target)}

    def workspace_info(self):
        if not self.ws:
            return None
        ctx = self.ws.context()[1]
        return {'name': self.ws.name, 'path': str(self.ws.root), 'source': self.report, 'context': ctx,
                'topics': self.ws.topic_count(), 'warning': self.ws_warning}

    def watch(self, srv, idle, startup):
        """桌面圖示啟動時（--auto-exit）：視窗全關、且沒有題目在跑，idle 秒後結束。
        一直沒有視窗連上（例如瀏覽器沒開成功）就等 startup 秒。
        迴圈一次跳過 30 秒以上，代表電腦剛睡醒：重新計時，等視窗重新報到，不要一醒就自殺。"""
        alive = prev = time.monotonic()
        while True:
            time.sleep(2)
            now = time.monotonic()
            if now - prev > 30 or self.windows > 0 or self.busy():
                alive = now
            prev = now
            if now - alive > (idle if self.ever_opened else startup):
                print(f'{now_str()} 卷證視窗都關了，伺服器結束。', flush=True)
                srv.shutdown()
                return

    # ---------------------------------------------------------------- 問答
    def ask(self, tid, body):
        question = (body.get('question') or '').strip()
        model, effort, scope = body.get('model'), body.get('effort'), body.get('scope', 'all')
        if not question:
            raise ValueError('問題是空的')
        self.need_ws()
        if model not in MODELS or effort not in EFFORTS or scope not in self.corpus.scopes:
            raise ValueError('模型、effort 或範圍不正確')
        topic = self.store.get(tid)
        with self.lock:
            if self.active and not self.jobs[self.active].done:
                raise RuntimeError('已經有一題正在研究，請等它完成或先停止。')
            job = Job('j' + secrets.token_hex(6), tid)
            self.jobs[job.id], self.active = job, job.id
        turn = {'id': 'u' + secrets.token_hex(4), 'q': question, 'model': model, 'effort': effort,
                'scope': scope, 'asked': now(), 'status': 'running'}
        if topic['title'] == '未命名題目' and not topic['turns']:
            topic['title'] = question[:40]
        topic['turns'].append(turn)
        self.store.save(topic)
        threading.Thread(target=self._work, args=(job, tid, turn['id']), daemon=True).start()
        return {'job': job.id, 'turn': turn['id']}

    def _work(self, job, tid, turn_id):
        topic = self.store.get(tid)
        turn = next(t for t in topic['turns'] if t['id'] == turn_id)
        before = self.runner.plan_usage(max_age=30)      # 查用量不耗額度；前後相減＝這題大約用了多少
        try:
            r = self.runner.run(job, topic, turn['q'], turn['model'], turn['effort'], turn['scope'])
        except Exception as ex:   # 什麼錯都要回報給畫面，不能讓前端永遠轉圈
            from agent import _Stopped
            stopped = isinstance(ex, _Stopped)
            topic = self.store.get(tid)
            turn = next(t for t in topic['turns'] if t['id'] == turn_id)
            turn.update(status='stopped' if stopped else 'error',
                        error=None if stopped else (str(ex) or type(ex).__name__),
                        secs=round(time.time() - job.started))
            self.store.save(topic)
            job.emit('stopped' if stopped else 'error', turn=turn, message=turn.get('error') or '已停止')
            return
        topic = self.store.get(tid)
        turn = next(t for t in topic['turns'] if t['id'] == turn_id)
        read_all = set(r['read'])
        for t in topic['turns']:
            read_all.update(t.get('read', []))
        blocks, cites = render.parse(r['answer'], self.corpus, read_all)
        turn.update(status='done', answer=r['answer'], blocks=blocks, cites=cites, read=r['read'],
                    trail=r['trail'], secs=r['secs'], usage=r['usage'], model_seen=r['model_seen'],
                    searches=sum(1 for s in r['trail'] if s[0] == 'search'), reads=len(r['read']))
        after = self.runner.plan_usage(max_age=0)
        turn['plan'] = plan_delta(before, after)
        topic['session_id'] = r['session_id']
        self.store.save(topic)
        job.emit('usage', **after)          # 要在 done 之前：前端收到 done 就關掉串流
        job.emit('done', turn=turn)

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        job.cancel.set()
        return {'ok': True}

    # ---------------------------------------------------------------- 核對與頁面
    def verdict(self, body):
        tid, turn_id, n = body.get('topic'), body.get('turn'), body.get('n')
        verdict, note = body.get('verdict'), (body.get('note') or '').strip()
        if verdict not in ('ok', 'bad', 'unsure', None):
            raise ValueError('verdict 只能是 ok／bad／unsure')
        topic = self.store.get(tid)
        turn = next(t for t in topic['turns'] if t['id'] == turn_id)
        c = turn['cites'][int(n)]
        c['verdict'], c['note'] = verdict, note
        self.store.save(topic)
        p = self.corpus.pages.get(c.get('page'))
        if verdict and p and p.get('cid'):           # 只有轉寫頁有 cid；PDF／文字檔的判斷只存在題目裡
            ev.op_log({'record': {'pageId': p['cid'], 'cite': c['page'], 'quote': c.get('quote', ''),
                                  'match': c.get('status'), 'verdict': verdict, 'note': note,
                                  'source': 'research_shell', 'topic': tid, 'turn': turn_id}})
        return {'ok': True}

    def topic(self, tid):
        """讀題目，並標出引文核對之後原文變了的（stale）或頁不見了的（gone）——只標在回應裡，不寫回檔案。
        2026-10-17 以前存的引文沒有 sha，不檢查。"""
        t = self.store.get(tid)
        for turn in t['turns']:
            for c in turn.get('cites') or []:
                if not c.get('sha'):
                    continue
                p = self.corpus.pages.get(c.get('page'))
                if not p:
                    c['gone'] = True
                elif p['sha'] != c['sha']:
                    c['stale'] = True
        return t

    def page(self, pid):
        self.need_ws()
        if pid not in self.corpus.pages:
            raise KeyError(pid)
        p = self.corpus.pages[pid]
        out = self.corpus.read(pid)
        if p.get('image_file') and self.ws.source in pathlib.Path(p['image_file']).parents:
            rel = pathlib.Path(p['image_file']).relative_to(self.ws.source).as_posix()
        elif p.get('pdf_file') and p['kind'] != 'pdf':    # 文件資料夾的掃描頁：原件 PDF 的第幾頁
            rel = f"{p['folder']}/{pathlib.Path(p['pdf_file']).name}（第 {p['pdf_page']} 頁）"
        else:
            rel = p.get('rel') or p.get('folder')
        out.update(cid=p.get('cid'), rel=rel, has_image=self.corpus.has_image(pid),
                   origin=self.corpus.origin(pid), no_text=bool(p.get('no_text')), sha=p['sha'])
        return out

    def image(self, pid, width):
        self.need_ws()
        if pid not in self.corpus.pages:
            raise KeyError(pid)
        if not width:
            return self.corpus.image(pid, 0)
        key = (pid, width)
        if key not in self.img_cache:
            data = self.corpus.image(pid, width)
            if len(self.img_cache) >= IMG_CACHE_MAX:
                self.img_cache.pop(next(iter(self.img_cache)))
            self.img_cache[key] = data
        return self.img_cache[key]

    def open_original(self, pid):
        f = self.corpus.original_file(pid) if self.ws and pid in self.corpus.pages else None
        if not f:
            raise KeyError(pid)
        return ev.op_open({'path': str(f)})

    def export(self, tid):
        t = self.store.get(tid)
        label = {'exact': '逐字吻合', 'variant': '僅異體字不同', 'near': '相近', 'miss': '原文裡找不到',
                 'unread': '未讀過此頁', 'cite': '頁碼', 'nopage': '語料無此頁', 'uncited': '未附出處'}
        vlabel = {'ok': '✓ 相符', 'bad': '✗ 有誤', 'unsure': '? 看不清'}
        lines = [f"# {t['title']}", '', f"> 卷證匯出 {now()}（研究專案：{self.ws.name}）。引文核對只代表"
                 '「這段字在 source 的文字裡」（轉寫或 PDF 文字層），不代表讀對了；請以原件為準。', '']
        for i, u in enumerate([u for u in t['turns'] if u.get('status') == 'done'], 1):
            lines += [f"## {i}. {u['q']}", '',
                      f"*{MODELS.get(u['model'], u['model'])}・{u['effort']}・{u['asked'][:16].replace('T', ' ')}*", '',
                      u['answer'], '', '### 引文核對', '']
            for c in u.get('cites', []):
                s = f"- 【{c.get('page') or '無出處'}】{label.get(c['status'], c['status'])}"
                if c.get('quote'):
                    s += f"：「{c['quote']}」"
                if c.get('verdict'):
                    s += f"　人工：{vlabel[c['verdict']]}"
                if c.get('note'):
                    s += f"（{c['note']}）"
                lines.append(s)
            lines.append('')
        return '\n'.join(lines)

    def status(self):
        auth = self.runner.check_auth(max_age=60)
        return {'auth': auth, 'version': VERSION, 'models': [{'id': k, 'label': v} for k, v in MODELS.items()],
                'efforts': list(EFFORTS), 'limits': LIMIT_MINUTES,
                'scopes': self.corpus.scopes if self.ws else {'all': '全部'},
                'corpus': self.corpus.summary() if self.ws else None,       # launch.pyw 認這個鍵判斷是不是卷證
                'workspace': self.workspace_info(), 'ws_error': self.ws_error,
                'recent': self.registry.listing(), 'legacy': len(workspace.legacy_topics(LEGACY_TOPICS)) if PAGES_JSONL else 0,
                'rate': self.runner.rate, 'windows': self.windows,
                'busy': ({'job': self.active, 'topic': self.jobs[self.active].topic_id}
                         if self.active and not self.jobs[self.active].done else None)}


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = 'juanzheng/1'

    def log_message(self, fmt, *args):
        # 只記錯誤；正常的 200 與靜態檔不洗版
        line = fmt % args
        if not re.search(r'"\s(200|304)\s', line):
            sys.stderr.write(f'{self.address_string()} {line}\n')

    # 只接受本機來的請求：Host 必須是 localhost／127.0.0.1（擋 DNS rebinding），
    # 寫入類請求的 Origin 若有帶，也必須是本機。
    def local_only(self):
        host = (self.headers.get('Host') or '').split(':')[0]
        if host not in ('127.0.0.1', 'localhost'):
            return False
        origin = self.headers.get('Origin')
        if self.command != 'GET' and origin:
            return urllib.parse.urlparse(origin).hostname in ('127.0.0.1', 'localhost')
        return True

    def send(self, code, body, ctype='application/json; charset=utf-8', headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def route(self, method):
        if not self.local_only():
            return self.send(403, {'error': '只接受本機連線'})
        url = urllib.parse.urlparse(self.path)
        path, qs = urllib.parse.unquote(url.path), urllib.parse.parse_qs(url.query)
        app = self.app
        # 會改資料或觸發動作的路徑只收 POST：GET 可以被別的網頁用 <img> 之類的標籤順手觸發
        mutating = (re.fullmatch(r'/api/(topics/\w+/ask|jobs/\w+/cancel|verdict|open|focus|workspace/\w+)', path)
                    or (path.startswith('/api/topics') and not path.endswith('/export') and method == 'POST'))
        if mutating and method != 'POST':
            return self.send(405, {'error': '這個動作只接受 POST'})
        try:
            if method == 'GET' and not path.startswith('/api/'):
                return self.static(path)
            if method == 'GET' and path == '/api/status':
                return self.send(200, app.status())
            if method == 'GET' and path == '/api/usage':
                return self.send(200, app.runner.plan_usage(max_age=0 if qs.get('refresh') else 60))
            if m := re.fullmatch(r'/api/workspace/(\w+)', path):
                return self.send(200, self.workspace_action(m.group(1), self.body()))
            if path.startswith('/api/topics'):
                if path == '/api/topics' and method == 'GET':
                    return self.send(200, app.store.list() if app.ws else [])
                app.need_ws()
            if path == '/api/topics':
                return self.send(200, app.store.create(self.body().get('title', '')))
            if m := re.fullmatch(r'/api/topics/(\w+)', path):
                if method == 'GET':
                    return self.send(200, app.topic(m.group(1)))
                t = app.store.get(m.group(1))
                t['title'] = (self.body().get('title') or t['title']).strip()[:80]
                return self.send(200, app.store.save(t))
            if m := re.fullmatch(r'/api/topics/(\w+)/ask', path):
                return self.send(200, app.ask(m.group(1), self.body()))
            if m := re.fullmatch(r'/api/topics/(\w+)/export', path):
                t = app.store.get(m.group(1))
                name = re.sub(r'[\\/:*?"<>|\s]+', '_', t['title'])[:40] + '.md'
                return self.send(200, app.export(m.group(1)).encode('utf-8'), 'text/markdown; charset=utf-8',
                                 {'Content-Disposition': "attachment; filename*=UTF-8''" + urllib.parse.quote(name)})
            if m := re.fullmatch(r'/api/jobs/(\w+)/events', path):
                return self.stream(m.group(1))
            if path == '/api/presence':
                return self.presence()
            if path == '/api/focus' and method == 'POST':
                if app.on_focus:
                    app.on_focus()
                return self.send(200, {'ok': bool(app.on_focus)})
            if m := re.fullmatch(r'/api/jobs/(\w+)/cancel', path):
                return self.send(200, app.cancel(m.group(1)))
            if path == '/api/verdict':
                return self.send(200, app.verdict(self.body()))
            # 頁碼可能是 1017/image00136，也可能是「文件代號/p3」（中文、底線）；存在與否由 corpus 判斷
            if m := re.fullmatch(r'/api/page/(.+)', path):
                return self.send(200, app.page(m.group(1)))
            if m := re.fullmatch(r'/api/img/(.+)', path):
                width = int(qs.get('w', ['1400'])[0] or 0)
                data, ctype = app.image(m.group(1), min(width, 3000))
                # 不同專案可能有同一個頁碼：前端在網址帶 ws=<專案路徑>，瀏覽器快取才不會跨專案錯用
                return self.send(200, data, ctype, {'Cache-Control': 'max-age=3600'})
            if path == '/api/open':
                return self.send(200, app.open_original(self.body().get('page')))
            return self.send(404, {'error': '沒有這個路徑'})
        except KeyError as ex:
            return self.send(404, {'error': f'找不到：{ex}'})
        except (ValueError, PermissionError, workspace.NotAProject) as ex:
            return self.send(400, {'error': str(ex)})
        except RuntimeError as ex:
            return self.send(409, {'error': str(ex)})
        except OSError as ex:                          # 建資料夾沒權限、檔案被鎖住…
            return self.send(400, {'error': f'{type(ex).__name__}：{ex}'})

    def workspace_action(self, action, body):
        app = self.app
        if action == 'open':
            return app.open_workspace(body.get('path') or '', init=bool(body.get('init')))
        if action == 'create':
            return app.create_workspace(body.get('parent') or '', body.get('name'))
        if action == 'reload':
            return app.reload_workspace()
        if action == 'forget':
            app.registry.forget(body.get('path') or '')
            return {'ok': True}
        if action == 'reveal':
            return app.reveal(body.get('sub'))
        if action == 'migrate':
            return app.migrate(body.get('parent') or '', body.get('name'))
        if action == 'import':
            return app.import_pack(body.get('path') or '', body.get('images'))
        raise KeyError(action)

    def static(self, path):
        rel = 'index.html' if path in ('', '/') else path.lstrip('/')
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self.send(404, {'error': '沒有這個檔案'})
        ctype = mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript', 'image/svg+xml'):
            ctype += '; charset=utf-8'
        return self.send(200, f.read_bytes(), ctype)

    def stream(self, job_id):
        """SSE：先補送已發生的事件，再即時推送，直到 done／error／stopped。"""
        job = self.app.jobs.get(job_id)
        if not job:
            return self.send(404, {'error': '沒有這個問答'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        i = 0
        try:
            while True:
                with job.cond:
                    while i >= len(job.events) and not job.done:
                        if not job.cond.wait(timeout=15):
                            break
                    batch, finished = job.events[i:], job.done
                i += len(batch)
                payload = ''.join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in batch) or ': ping\n\n'
                self.wfile.write(payload.encode('utf-8'))
                self.wfile.flush()
                if finished and i >= len(job.events):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def presence(self):
        """每個卷證視窗開著一條這種連線；視窗一關，下次寫入就失敗，計數減一。
        用長連線而不是定時報到：視窗縮小時瀏覽器會把計時器壓到一分鐘一次，定時報到會誤判。"""
        app = self.app
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with app.lock:
            app.windows += 1
            app.ever_opened = True
        try:
            while True:
                self.wfile.write(b': alive\n\n')
                self.wfile.flush()
                time.sleep(5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with app.lock:
                app.windows -= 1

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_HEAD(self):   # 健康檢查用；不帶內容
        self.send_response(200 if self.local_only() else 403)
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description='卷證：本機文獻研究 shell')
    ap.add_argument('--port', type=int, default=int(os.environ.get('JUANZHENG_PORT') or 8790))
    ap.add_argument('--auto-exit', action='store_true', help='卷證視窗全關後自動結束（桌面圖示啟動用）')
    ap.add_argument('--idle', type=int, default=20, help='最後一個視窗關掉後等幾秒才結束')
    ap.add_argument('--startup', type=int, default=120, help='啟動後等第一個視窗連上的秒數')
    ap.add_argument('--data', help='改用別的執行期資料夾（最近專案清單、快取、舊題目）；開發測試用，'
                                   '免得動到真的執行期資料（位置見 paths.py）')
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if s and hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    if a.data:
        use_data_dir(a.data)
    Handler.app = App()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    srv.daemon_threads = True
    print(f'{now_str()} 卷證：http://127.0.0.1:{a.port}/' + ('（視窗全關後自動結束）' if a.auto_exit else ''),
          flush=True)
    if a.auto_exit:
        threading.Thread(target=Handler.app.watch, args=(srv, a.idle, a.startup), daemon=True).start()
    srv.serve_forever()


if __name__ == '__main__':
    main()
