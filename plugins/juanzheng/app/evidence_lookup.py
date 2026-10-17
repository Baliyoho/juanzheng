#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""證據面板的資料端：給一頁＋一句引文，找出它在原圖與轉寫裡的位置。

`.claude/skills/evidence-pane/`（Claude Code mod）呼叫本檔；也可單獨在命令列用。

## 為什麼原文由這裡讀，不讓模型自己貼

證據面板的用途是**核對**。如果「原文」欄也是模型給的，它改寫過、記錯或
編造的句子會原樣出現在兩欄，看起來完全吻合——核對就失去意義。所以模型只交
「頁碼＋引文」，原文一律由本檔從語料檔讀出；引文在原文找不到時明白標示，
**不做靜默的模糊放行**（模糊比對只當作「最接近的段落」提示）。

## 輸入（stdin 一個 JSON 物件）

- `{"op": "lookup", "page": "<卷號/imageNNNNN>", "quote": "<引文>"}`
  page 也收 `【29/image00010】`、page_id（16 位十六進位）、`sample/` 下的相對路徑、
  測試集正解檔檔名（舊的 150 頁工作區）。
- `{"op": "open", "path": "<影像路徑>"}`：用系統看圖程式開原圖
- `{"op": "log", "record": {...}}`：核對結果附加到 `data/evidence/verdicts.jsonl`

輸出：stdout 一個 JSON 物件。失敗時 `{"error": "..."}` 且 exit code 非 0。

## 原文從哪裡來

主鍵與引用格式照 [citation-protocol.md](../docs/ai-notes/citation-protocol.md)：
`data/panel/index.json`（`build_corpus.py` 產生）把 `卷號/imageNNNNN` 對到 `rel` 與
`cid`，正文讀 `data/corpus/<卷號>/<image>.txt`（跳過固定檔頭）。轉寫模型就是語料的
那一個（目前 `gemini-3.1-pro-preview@v3`），2026-09 起面板「只留 3.1 Pro」的決定照舊。
`data/results/` 底下其他模型目錄只計數，不並陳；`--models` 可加。

另外兩種只出現在 150 頁測試集的版本：
- `reviewed`：`groundtruth_printed/*_正解.txt` 且內容已不同於預填草稿（`draft_sha`）
- `draft`：同一檔但**尚未被人改過**——那其實是某個模型的輸出，不可當人工正解

## 原圖

v3 沒有座標，所以給整頁。面板的 Svg 元件一份上限約 13 萬字元，整頁塞一張會糊到
讀不出字，因此**切成數條橫條、各自壓在上限內**，面板依序疊起來。若某個 v2 結果
（只有測試集頁有）逐字命中且帶框，就只裁那一段。

不連網、不需要憑證。依賴 Pillow 與 rapidfuzz（`run_ocr.py` 已在用）。
"""
import argparse
import base64
import contextlib
import csv
import datetime
import hashlib
import io
import json
import math
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

RESULTS = pathlib.Path('data/results')
SRC = pathlib.Path('sample')
# 第一批 2026-09 搬進這一層，但結果 JSON 的 rel 還是舊路徑（HANDOFF 地雷）
SRC_FALLBACKS = (SRC, SRC / '第一批測試')
CORPUS = pathlib.Path('data/corpus')
PANEL_INDEX = pathlib.Path('data/panel/index.json')
GT_DIR = pathlib.Path('data/testset/groundtruth_printed')
LOG = pathlib.Path('data/evidence/verdicts.jsonl')
CONTEXT = 40          # 命中處前後各取幾個字
FUZZY_MIN = 80        # rapidfuzz partial_ratio 低於此值視為「找不到」
# 面板 Svg 元件整份 markup 上限 131072 字元；base64 膨脹 4/3，
# 再留 SVG 外殼的餘裕，每條影像本體須 ≤ 約 90 KB
TILE_MAX_BYTES = 90_000
PAGE_WIDTH = 1400     # 整頁縮到這個寬度再切條；直式頁約 4–6 條
MAX_TILES = 12
try:
    from PIL import features as _features
    HAS_WEBP = bool(_features.check('webp'))
except ImportError:
    HAS_WEBP = False


def cid(rel):
    """rel → page_id，與 run_ocr.py／build_corpus.py 同一套雜湊。"""
    return hashlib.blake2s(rel.encode('utf-8'), digest_size=8).hexdigest()


def squash(t):
    """比對用：拿掉所有空白與換行。直書轉寫的斷行位置各模型不同，不代表文字不同。
    回傳壓縮後字串，以及每個字元在原字串的位置（用來把命中處還原到原文）。"""
    chars, pos = [], []
    for i, ch in enumerate(t or ''):
        if not ch.isspace():
            chars.append(ch)
            pos.append(i)
    return ''.join(chars), pos


# 標點與數字的全形／半形不算文字差異：模型回答常用半形逗號，轉寫是全形（2026-10-08 卷證
# 首次實測，Sonnet 的引文因此全部只得「相近」）。一對一替換，不改字數，命中位置不位移。
# 不收「?」——`[?]` 是轉寫的讀不出記號，要原樣比對。
WIDTH = str.maketrans({',': '，', ';': '；', ':': '：', '!': '！', '(': '（', ')': '）',
                       **{chr(0xFF10 + i): str(i) for i in range(10)}})


def norm_marks(t):
    """`〔?〕`→`[?]`（協定 3.4 的規約偏差）；`@@PAGEBREAK` 換成等長空白，
    不讓分頁記號擋在跨頁引文中間，位置也不位移；標點、數字統一全半形。"""
    t = (t or '').replace('〔?〕', '[?]').replace('（?）', '[?]')
    return t.replace('@@PAGEBREAK', ' ' * len('@@PAGEBREAK')).translate(WIDTH)


# ---------------------------------------------------------------- 讀取各版本

def load_panel_index():
    if not PANEL_INDEX.exists():
        return None
    return json.loads(PANEL_INDEX.read_text(encoding='utf-8'))


def load_gt_index():
    """測試集正解工作區 index.csv：rel → 檔名 stem 與 draft_sha。沒有工作區就回空。"""
    idx = GT_DIR / 'index.csv'
    out = {}
    if not idx.exists():
        return out
    with idx.open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            stem = (f"{int(r['order']):03d}_{r['difficulty']}_f{r['fonds']}_"
                    f"{pathlib.PurePosixPath(r['rel'].replace(chr(92), '/')).stem}")
            out[r['rel']] = {'stem': stem, 'draft_sha': r.get('draft_sha', '')}
    return out


def resolve_page(page, panel, gt_index):
    """回傳 dict：pageId、rel、cite（`卷號/image` 或 None）、fonds、image。
    不論用哪種寫法找到，只要那頁在語料裡，都補回 `卷號/image`——語料正文靠它讀。"""
    found = _resolve(page, panel, gt_index)
    if panel and not found['cite']:
        for fonds, f in panel['fonds'].items():
            for image, meta in (f.get('pages') or {}).items():
                if meta['rel'] == found['rel'] or meta['cid'] == found['pageId']:
                    return {'pageId': meta['cid'], 'rel': meta['rel'],
                            'cite': f'{fonds}/{image}', 'fonds': fonds, 'image': image}
    return found


def _resolve(page, panel, gt_index):
    p = (page or '').strip().strip('`"\'').strip('【】[]').strip()
    p = re.sub(r'\.(jpg|jpeg|txt|json)$', '', p, flags=re.I).replace('\\', '/')
    if not p:
        raise LookupError('沒有給頁碼')
    if panel and '/' in p:
        fonds, image = p.split('/', 1)
        meta = (panel['fonds'].get(fonds) or {}).get('pages', {}).get(image)
        if meta:
            return {'pageId': meta['cid'], 'rel': meta['rel'], 'cite': f'{fonds}/{image}',
                    'fonds': fonds, 'image': image}
        if fonds in panel['fonds']:
            raise LookupError(f'{fonds} 卷裡沒有 {image}（sample/ 編號本來就有缺口）')
    # 以下是測試集與舊路徑的退路
    for base in SRC_FALLBACKS:
        for ext in ('', '.jpg'):
            if (base / (p + ext)).is_file():
                rel = p + ext
                return {'pageId': cid(rel), 'rel': rel, 'cite': None, 'fonds': None, 'image': None}
    for rel, v in gt_index.items():
        if p == rel or p.startswith(v['stem']):
            return {'pageId': cid(rel), 'rel': rel, 'cite': None, 'fonds': None, 'image': None}
    if re.fullmatch(r'[0-9a-f]{16}', p):
        for d in sorted(RESULTS.glob('*')) if RESULTS.exists() else []:
            f = d / f'{p}.json'
            if f.exists():
                return {'pageId': p, 'rel': json.loads(f.read_text(encoding='utf-8'))['rel'],
                        'cite': None, 'fonds': None, 'image': None}
        raise LookupError(f'page_id {p} 在 {RESULTS} 下找不到任何結果檔')
    if panel and '/' in p:
        raise LookupError(f'語料裡沒有卷號 {p.split("/", 1)[0]}——可能是還沒轉寫的卷')
    raise LookupError(f'找不到頁面「{page}」；請用 卷號/imageNNNNN 格式（如 29/image00010）')


def parse_v2(text):
    """v2 輸出是區塊 JSON。沿用 overlay_proto.parse_blocks（含截斷救回）。
    它會 print 警告，導到 stderr 免得汙染本檔的 JSON 輸出。"""
    from overlay_proto import parse_blocks
    with contextlib.redirect_stdout(sys.stderr):
        return parse_blocks(text)


def corpus_source(page, panel):
    """語料檔正文（跳過固定檔頭）。沒有語料檔就退回該模型的結果 JSON。"""
    if not panel or not page['cite']:
        return None
    model = panel.get('model', '')
    f = CORPUS / page['fonds'] / f"{page['image']}.txt"
    if f.exists():
        lines = f.read_text(encoding='utf-8').splitlines()
        body = '\n'.join(lines[panel.get('header_lines', 7):])
        return {'name': model, 'kind': 'ocr', 'text': body, 'blocks': None}
    r = RESULTS / model / f"{page['pageId']}.json"
    if r.exists():
        rec = json.loads(r.read_text(encoding='utf-8'))
        if rec.get('output_text'):
            return {'name': model, 'kind': 'ocr', 'text': rec['output_text'], 'blocks': None}
    return None


def result_sources(page_id, shown_names, extra_models):
    """data/results/ 底下這頁的其他版本。回傳 (要並陳的, 只計數的)。"""
    shown, others = [], []
    if not RESULTS.exists():
        return shown, others
    for d in sorted(RESULTS.iterdir()):
        f = d / f'{page_id}.json'
        if not d.is_dir() or d.name in shown_names or not f.exists():
            continue
        r = json.loads(f.read_text(encoding='utf-8'))
        if r.get('error') or not r.get('output_text'):
            continue
        src = {'name': d.name, 'kind': 'ocr', 'text': r['output_text'], 'blocks': None}
        if '@v2' in d.name or r.get('prompt_version') == 'ocr_system_v2':
            blocks = parse_v2(r['output_text'])
            if blocks:
                src['blocks'] = blocks
                src['text'] = '\n'.join(b['text'] for b in blocks)
        (shown if d.name in extra_models else others).append(src)
    return shown, others


def gt_source(rel, gt_index):
    v = gt_index.get(rel)
    if not v:
        return None
    f = GT_DIR / f"{v['stem']}_正解.txt"
    if not f.exists():
        return None
    text = f.read_text(encoding='utf-8')
    sha = hashlib.blake2s(text.encode('utf-8'), digest_size=8).hexdigest()
    kind = 'draft' if v['draft_sha'] and sha == v['draft_sha'] else 'reviewed'
    return {'name': '正解' if kind == 'reviewed' else '正解（未審草稿）',
            'kind': kind, 'text': text, 'blocks': None}


# ---------------------------------------------------------------- 比對

# 一對一的異體字對照（左＝異體，右＝通行字）。逐字比對失敗時，兩邊都換成通行字
# 再比一次，命中就標 variant——和真正的錯字（實／暫）分開，但仍不算「逐字」：
# 引文要照原件用字。2026-10-05 實機：1005/image00013 寫「爲」，引文寫「為」。
# 只收一對一、不改變字數的對照，命中位置才能還原到原文。
VARIANTS = str.maketrans({
    '爲': '為', '僞': '偽', '衞': '衛', '裏': '裡', '綫': '線', '眞': '真', '卽': '即',
    '旣': '既', '槪': '概', '敎': '教', '着': '著', '册': '冊', '臺': '台', '戸': '戶',
    '户': '戶', '争': '爭', '擧': '舉', '温': '溫', '説': '說', '税': '稅', '内': '內',
    '呉': '吳', '黄': '黃', '靑': '青', '淸': '清', '硏': '研', '値': '值', '彫': '雕',
    '峯': '峰', '鷄': '雞', '敍': '敘', '○': '〇',
})


def locate(quote, text):
    """在 text 裡找 quote。回傳 dict：exact／variant／fuzzy／none，及原文中的起訖位置。"""
    q, _ = squash(norm_marks(quote))
    t, pos = squash(norm_marks(text))
    if not q or not t:
        return {'match': 'none'}
    i = t.find(q)
    if i >= 0:
        return {'match': 'exact', 'start': pos[i], 'end': pos[i + len(q) - 1] + 1}
    i = t.translate(VARIANTS).find(q.translate(VARIANTS))
    if i >= 0:
        return {'match': 'variant', 'start': pos[i], 'end': pos[i + len(q) - 1] + 1}
    from rapidfuzz import fuzz
    al = fuzz.partial_ratio_alignment(q, t)
    if al is None or al.score < FUZZY_MIN or al.dest_end <= al.dest_start:
        return {'match': 'none', 'score': round(al.score, 1) if al else 0}
    return {'match': 'fuzzy', 'score': round(al.score, 1),
            'start': pos[al.dest_start], 'end': pos[al.dest_end - 1] + 1}


def snippet(text, hit):
    if 'start' not in hit:
        return None
    s, e = hit['start'], hit['end']
    clean = lambda x: x.replace('@@PAGEBREAK', '〔換頁〕')
    return {'before': ('…' if s > CONTEXT else '') + clean(text[max(0, s - CONTEXT):s]),
            'match': clean(text[s:e]),
            'after': clean(text[e:e + CONTEXT]) + ('…' if e + CONTEXT < len(text) else '')}


def hit_boxes(src, hit):
    """命中範圍跨到的 v2 區塊框。區塊在 text 裡以換行串接，依序累計位移。"""
    if not src['blocks'] or 'start' not in hit:
        return []
    boxes, off = [], 0
    for b in src['blocks']:
        a, z = off, off + len(b['text'])
        if a < hit['end'] and z > hit['start']:
            boxes.append(b['box'])
        off = z + 1
    return boxes


# ---------------------------------------------------------------- 影像

def find_image(rel):
    for base in SRC_FALLBACKS:
        if (base / rel).is_file():
            return base / rel
    return None


def to_pixels(boxes, w, h):
    """座標慣例三家不同（2026-07-29）：影像都 ≥1805px，
    全部座標 ≤1000 必然是 0–1000 正規化；否則當像素。回傳 (x1, y1, x2, y2)。"""
    flat = [v for b in boxes for v in b]
    scale = flat and max(flat) <= 1000
    out = []
    for y1, x1, y2, x2 in boxes:
        if scale:
            y1, y2, x1, x2 = y1 * h / 1000, y2 * h / 1000, x1 * w / 1000, x2 * w / 1000
        out.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return out


def encode(im, quality):
    """WebP：同畫質比 JPEG 小約三成，桌面版（Chromium）的 SVG 影像吃得下。
    Pillow 沒編進 WebP 時退回 JPEG。回傳 (位元組, MIME)。"""
    b = io.BytesIO()
    if HAS_WEBP:
        im.save(b, 'WEBP', quality=quality, method=4)
        return b.getvalue(), 'image/webp'
    im.save(b, 'JPEG', quality=quality, optimize=True)
    return b.getvalue(), 'image/jpeg'


def tiles_of(im):
    """切成橫條，每條 ≤ TILE_MAX_BYTES。**能一張就一張**：桌面版把橫條上下疊時
    接縫會出現一道看起來像原件劃線的暗線（2026-10-05 實機看到），容易誤判。
    順序：整張 1400→1200→1000 px 各試兩種品質（面板實際只顯示約 700 px 寬，
    1000 px 在高解析螢幕上仍清楚）；都塞不下才切 1400 px 的條（條數由整張
    的大小估計）；最後才整體縮小。"""
    im = im.convert('RGB') if im.mode not in ('L', 'RGB') else im

    def fit(width):
        return im if im.width <= width else im.resize(
            (width, max(1, round(im.height * width / im.width))))

    def pack(img, n, quality):
        step = math.ceil(img.height / n)
        out = []
        for y in range(0, img.height, step):
            data, mime = encode(img.crop((0, y, img.width, min(img.height, y + step))), quality)
            if len(data) > TILE_MAX_BYTES:
                return None
            out.append({'width': img.width, 'height': min(step, img.height - y),
                        'data_uri': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')})
        return out

    # 先各編一次整張，能塞進一條就收工
    for width in (PAGE_WIDTH, 1200, 1000):
        img = fit(width)
        for quality in (70, 55):
            tiles = pack(img, 1, quality)
            if tiles:
                return tiles, img.size
    # 切條：用整張的大小估條數，不必從 2 條一路試上去（WebP 編碼一次就要上百毫秒）
    img = fit(PAGE_WIDTH)
    while True:
        whole, _ = encode(img, 60)
        start = max(2, math.ceil(len(whole) * 1.15 / TILE_MAX_BYTES))
        for n in range(start, min(MAX_TILES, start + 3) + 1):
            for quality in (60, 45):
                tiles = pack(img, n, quality)
                if tiles:
                    return tiles, img.size
        img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))))


def render_image(path, boxes):
    """有框就裁命中區塊（外擴 8%），沒有就整頁。回傳 dict 或 None。"""
    from PIL import Image
    if path is None:
        return None
    with Image.open(path) as im:
        im.load()
        w, h = im.size
        kind = 'page'
        if boxes:
            px = to_pixels(boxes, w, h)
            x1 = min(b[0] for b in px); y1 = min(b[1] for b in px)
            x2 = max(b[2] for b in px); y2 = max(b[3] for b in px)
            pad = 0.08 * max(w, h)
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
            if x2 - x1 > 8 and y2 - y1 > 8:
                im = im.crop((int(x1), int(y1), int(x2), int(y2)))
                kind = 'crop'
        tiles, (tw, th) = tiles_of(im)
    return {'kind': kind, 'width': tw, 'height': th, 'tiles': tiles}


# ---------------------------------------------------------------- 指令

def op_lookup(req, extra_models):
    quote = (req.get('quote') or '').strip()
    if not quote:
        raise LookupError('沒有給引文')
    panel = load_panel_index()
    gt_index = load_gt_index()
    page = resolve_page(req.get('page'), panel, gt_index)

    sources = []
    gt = gt_source(page['rel'], gt_index)
    if gt:
        sources.append(gt)
    corpus = corpus_source(page, panel)
    if corpus:
        sources.append(corpus)
    shown, others = result_sources(page['pageId'], {s['name'] for s in sources}, extra_models)
    sources += shown

    out_sources, hits = [], {}
    for s in sources:
        hit = locate(quote, s['text'])
        hits[id(s)] = hit
        out_sources.append({'name': s['name'], 'kind': s['kind'], 'match': hit['match'],
                            'score': hit.get('score'), 'snippet': snippet(s['text'], hit),
                            'hasBoxes': bool(s['blocks'])})
    tally = {'exact': 0, 'variant': 0, 'fuzzy': 0, 'none': 0}
    for s in others:
        hit = locate(quote, s['text'])
        hits[id(s)] = hit
        tally[hit['match']] += 1

    # 裁切用的框：優先逐字命中的 v2 版本，其次模糊命中
    boxes, box_from = [], None
    for want in ('exact', 'variant', 'fuzzy'):
        for s in sources + others:
            b = hit_boxes(s, hits[id(s)]) if hits[id(s)]['match'] == want else []
            if b:
                boxes, box_from = b, s['name']
                break
        if boxes:
            break

    img = find_image(page['rel'])
    return {'pageId': page['pageId'], 'rel': page['rel'], 'cite': page['cite'], 'quote': quote,
            'imagePath': str(img.resolve()) if img else None,
            'sources': out_sources, 'others': tally,
            'image': render_image(img, boxes), 'boxFrom': box_from}


def op_open(req):
    p = pathlib.Path(req.get('path') or '')
    if not p.is_file():
        raise LookupError(f'找不到影像：{p}')
    if sys.platform.startswith('win'):
        os.startfile(str(p))          # 只存在於 Windows
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(p)])
    else:
        subprocess.Popen(['xdg-open', str(p)])
    return {'opened': str(p)}


def op_log(req):
    rec = dict(req.get('record') or {})
    if not rec.get('pageId') or rec.get('verdict') not in ('ok', 'bad', 'unsure'):
        raise LookupError('紀錄缺 pageId 或 verdict 不是 ok／bad／unsure')
    rec['at'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return {'logged': str(LOG)}


def main():
    ap = argparse.ArgumentParser(description='證據面板資料端（讀 stdin JSON）')
    ap.add_argument('--models', default='',
                    help='除了語料模型之外，另外並陳的結果目錄（逗號分隔）；其餘只計數')
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    try:
        req = json.loads(sys.stdin.buffer.read().decode('utf-8') or '{}')
        op = req.get('op', 'lookup')
        if op == 'lookup':
            out = op_lookup(req, [m.strip() for m in a.models.split(',') if m.strip()])
        elif op == 'open':
            out = op_open(req)
        elif op == 'log':
            out = op_log(req)
        else:
            raise LookupError(f'未知的 op：{op}')
    except (LookupError, OSError, ValueError, KeyError) as ex:
        print(json.dumps({'error': f'{type(ex).__name__}: {ex}' if isinstance(ex, KeyError) else str(ex)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
