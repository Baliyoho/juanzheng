"""卷證的命令列查詢：讓 Claude Code（外掛的 research skill）在終端機裡搜尋、讀頁、核對引文。

    python cli.py [--project 資料夾] search 關鍵詞 [關鍵詞…] [--volume 卷] [--limit 10]
    python cli.py [--project 資料夾] read 1017/image00136
    python cli.py [--project 資料夾] catalog
    python cli.py [--project 資料夾] context
    python cli.py [--project 資料夾] verify 1017/image00136 "逐字引文"
    python cli.py projects

與卷證視窗用同一套 Corpus（同樣的搜尋、讀頁與引文核對規則），輸出 JSON，**不呼叫模型、不耗額度**。
沒給 --project 就用最近在卷證開過的那個專案。只讀不寫：不動 source、不寫研究題目。
"""
import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402
import sources  # noqa: E402
import workspace  # noqa: E402
from corpus import Corpus  # noqa: E402


VERDICT_LABEL = {'exact': '逐字吻合', 'variant': '僅異體字不同', 'near': '相近（有字不同，不能當逐字引文）',
                 'miss': '原文裡找不到'}


def out(value):
    print(json.dumps(value, ensure_ascii=False, indent=1))


def pick_project(arg):
    reg = workspace.Registry(paths.DATA / 'workspaces.json').load()
    path = arg or reg['current'] or next(iter(reg['recent']), None)
    if not path:
        raise SystemExit('還沒有研究專案：先在卷證視窗開一個，或用 --project 指定專案資料夾。')
    info = workspace.inspect(path)
    if not info['is_dir'] or info['missing']:
        raise SystemExit(f'{path} 不是卷證研究專案（要有 source／context／session 三個子資料夾）。')
    return workspace.Workspace(path)


def load(ws):
    records, report = sources.scan(ws.source, paths.DATA / 'cache')
    return Corpus(records), report


def page_id(corpus, raw):
    pid = corpus.normalize_id(raw)
    if not pid or pid not in corpus.pages:
        raise SystemExit(f'語料裡沒有 {raw}；請用 search／catalog 回傳的頁碼。')
    return pid


def main():
    for s in (sys.stdout, sys.stderr):
        if s and hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='卷證：命令列查詢研究專案的 source（不呼叫模型）')
    ap.add_argument('--project', help='研究專案資料夾；不給就用最近在卷證開過的')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('search', help='全文搜尋（多詞任一命中即列出，全部命中者排前）')
    s.add_argument('terms', nargs='+')
    s.add_argument('--volume', help='只搜這一卷（文件）')
    s.add_argument('--limit', type=int, default=10)
    s = sub.add_parser('read', help='讀一頁全文（含前後頁頁碼）')
    s.add_argument('page')
    sub.add_parser('catalog', help='列出各卷（文件）的代號、標題、頁數')
    sub.add_parser('context', help='專案 context/ 的研究指示與筆記（二手資料，不能當出處）')
    s = sub.add_parser('verify', help='核對一段「」引文是否逐字出現在該頁')
    s.add_argument('page')
    s.add_argument('quote')
    sub.add_parser('projects', help='最近在卷證開過的專案')
    a = ap.parse_args()

    if a.cmd == 'projects':
        reg = workspace.Registry(paths.DATA / 'workspaces.json')
        return out({'current': reg.load()['current'], 'recent': reg.listing()})
    ws = pick_project(a.project)
    if a.cmd == 'context':
        text, info = ws.context()
        return out({'project': str(ws.root), 'context': info, 'text': text})
    corpus, report = load(ws)
    if a.cmd == 'search':
        r = corpus.search(' '.join(a.terms)[:200], 'all', a.volume, max(1, min(a.limit, 50)))
        return out({'project': str(ws.root), **r})
    if a.cmd == 'read':
        return out(corpus.read(page_id(corpus, a.page)))
    if a.cmd == 'catalog':
        return out({'project': str(ws.root), 'summary': corpus.summary(), 'warnings': report.get('warnings'),
                    'catalog': corpus.catalog('all')})
    if a.cmd == 'verify':
        pid = page_id(corpus, a.page)
        v = corpus.verify(a.quote, pid)
        return out({'page': pid, 'label': VERDICT_LABEL.get(v['status'], v['status']),
                    'text_origin': corpus.pages[pid].get('text_origin'), **v})


if __name__ == '__main__':
    sys.exit(main())
