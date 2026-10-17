"""卷證的工作區：一個研究專案＝一個資料夾，底下固定三個子資料夾。

- `source/`：研究材料。任意文件，怎麼讀見 sources.py；模型只能搜尋、讀這裡的東西。
- `context/`：研究指示與背景筆記。根目錄的 .md／.txt 每題開跑前自動放進給 Claude 的指示
  （改了下一題就生效）；子資料夾與其他格式不讀。筆記是二手資料，不能當出處引用。
- `session/`：研究題目，一題一個 JSON（問答、研究足跡、引文核對與人工判斷）。

專案資料夾放哪裡都可以（桌面、雲端硬碟…），可以整包搬走；「最近開過的專案」記在這台電腦的
`data/research_shell/workspaces.json`，不跟專案走。
"""
import datetime
import json
import os
import pathlib
import shutil
import subprocess

import sources

SUBDIRS = ('source', 'context', 'session')
CONTEXT_EXT = ('.md', '.markdown', '.txt')
CONTEXT_MAX = 40_000            # context 一次帶入的字數上限；超過的截斷並在畫面上標出
RECENT_MAX = 12


class NotAProject(Exception):
    pass


def inspect(path):
    """還沒開之前先看一眼：資料夾在不在、缺哪幾個子資料夾、裡面已經有多少東西。"""
    p = pathlib.Path(path).expanduser()
    out = {'path': str(p), 'exists': p.exists(), 'is_dir': p.is_dir(), 'missing': list(SUBDIRS), 'entries': 0}
    if p.is_dir():
        out['missing'] = [d for d in SUBDIRS if not (p / d).is_dir()]
        out['entries'] = sum(1 for x in p.iterdir() if x.name not in SUBDIRS)
    return out


class Workspace:
    def __init__(self, root):
        self.root = pathlib.Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f'找不到資料夾：{self.root}')
        missing = [d for d in SUBDIRS if not (self.root / d).is_dir()]
        if missing:
            raise NotAProject(f"不是卷證專案（缺 {'、'.join(missing)}）：{self.root}")

    @classmethod
    def init(cls, path):
        """建立（或補齊）專案資料夾。已有的檔案一律不動。"""
        root = pathlib.Path(path).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        for d in SUBDIRS:
            (root / d).mkdir(exist_ok=True)
        return cls(root)

    name = property(lambda self: self.root.name)
    source = property(lambda self: self.root / 'source')
    context_dir = property(lambda self: self.root / 'context')
    session = property(lambda self: self.root / 'session')

    def context(self, limit=CONTEXT_MAX):
        """回傳 (要放進指示的文字, 給畫面看的說明)。依檔名排序，總長超過 limit 就截斷。"""
        files, ignored, parts, used = [], [], [], 0
        for f in sorted(self.context_dir.iterdir(), key=lambda x: x.name) if self.context_dir.is_dir() else []:
            if f.name.startswith(('.', '~$')):
                continue
            if f.is_dir() or f.suffix.lower() not in CONTEXT_EXT:
                ignored.append(f.name + ('/' if f.is_dir() else ''))
                continue
            try:
                text = sources.read_text(f)[0].strip()
            except OSError as ex:
                files.append({'name': f.name, 'chars': 0, 'used': 0, 'error': str(ex)})
                continue
            take = text[:max(0, limit - used)]
            files.append({'name': f.name, 'chars': len(text), 'used': len(take), 'truncated': len(take) < len(text)})
            if take:
                parts.append(f'### {f.name}\n\n{take}')
                used += len(take)
        return '\n\n'.join(parts), {'files': files, 'ignored': ignored, 'used': used, 'max': limit}

    def topic_count(self):
        return sum(1 for _ in self.session.glob('t*.json'))


def git_warning(root, repo):
    """專案放在 repo 裡、又沒被 .gitignore 擋住時提醒：研究題目含史料內容，不能 commit（鐵律 1）。"""
    root, repo = pathlib.Path(root).resolve(), pathlib.Path(repo).resolve()
    if root != repo and repo not in root.parents:
        return None
    try:
        r = subprocess.run(['git', '-C', str(repo), 'check-ignore', '-q', str(root / 'session')],
                           capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return None
    return '這個專案資料夾在程式的 Git repo 裡、沒被 .gitignore 擋住；研究題目含史料內容，可能被誤 commit。建議搬到 repo 外。'


class Registry:
    """這台電腦最近開過的專案（最新的在前）與目前開著的那一個。"""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def load(self):
        try:
            d = json.loads(self.path.read_text(encoding='utf-8'))
            return {'current': d.get('current'), 'recent': list(d.get('recent') or [])}
        except (OSError, ValueError):
            return {'current': None, 'recent': []}

    def save(self, d):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(self.path)

    def touch(self, root):
        d, root = self.load(), str(root)
        d['recent'] = [root] + [p for p in d['recent'] if os.path.normcase(p) != os.path.normcase(root)]
        d['recent'], d['current'] = d['recent'][:RECENT_MAX], root
        self.save(d)

    def forget(self, root):
        d = self.load()
        d['recent'] = [p for p in d['recent'] if os.path.normcase(p) != os.path.normcase(str(root))]
        if d['current'] and os.path.normcase(d['current']) == os.path.normcase(str(root)):
            d['current'] = None
        self.save(d)

    def listing(self):
        d = self.load()
        return [{'path': p, 'name': pathlib.Path(p).name, 'ok': not inspect(p)['missing']} for p in d['recent']]


def legacy_topics(legacy_dir):
    d = pathlib.Path(legacy_dir)
    return sorted(d.glob('t*.json')) if d.is_dir() else []


def migrate(legacy_dir, ws, corpus_file):
    """把改版前的研究題目（data/research_shell/topics/）搬進專案：
    1. source/ 沒有任何轉寫總表，就把 corpus_file 依卷拆成文件資料夾（importer.split_table；頁碼不變，
       舊題目的引文照樣對得上）並寫 _catalog.csv；
    2. 題目逐檔複製到 session/，同名的不覆蓋；
    3. 舊資料夾改名成 topics.migrated-<時間>，留著當備份，不刪。"""
    import importer
    legacy_dir, corpus_file = pathlib.Path(legacy_dir), pathlib.Path(corpus_file)
    files = legacy_topics(legacy_dir)
    if not files:
        raise ValueError('沒有要搬的舊題目')
    split = None
    has_table = any(sources.is_pages_jsonl(f) for f in ws.source.rglob('*.jsonl'))
    if not has_table and corpus_file.is_file():
        split = importer.split_table(corpus_file, ws.source)
    moved, kept = [], []
    for f in files:
        dest = ws.session / f.name
        if dest.exists():
            kept.append(f.name)
            continue
        shutil.copy2(f, dest)
        moved.append(f.name)
    backup = legacy_dir.with_name(f"{legacy_dir.name}.migrated-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    legacy_dir.rename(backup)
    return {'moved': len(moved), 'kept': kept, 'volumes': split['made'] if split else 0, 'backup': str(backup)}
