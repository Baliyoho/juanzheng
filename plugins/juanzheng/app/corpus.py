"""卷證的語料層：一個研究專案 source/ 裡所有文件的頁、全文搜尋、讀頁、引文比對、原圖。

頁從哪來見 sources.py 與規格 docs/ai-notes/source-format.md。頁碼兩種寫法：
- 檔案管理局轉寫：`1017/image00136`（卷號／影像檔名），沿用整個專案的引用慣例。
- 其他文件：`文件代號/p3`（PDF 第 3 頁，或文字檔第 3 段）。
重複掃描、注意事項、文字來源這些文件層級的事實來自 source 的 `_catalog.csv`，**不寫在程式裡**。

引文比對**不另寫一套**：直接用 `tools/evidence_lookup.py` 的 `locate()`（逐字→異體字→
rapidfuzz 相近→找不到），讓 Claude Code 證據面板 mod 與本工具的判定永遠一致。
原文一律由這裡從語料讀出，不採用模型貼回來的「原文」——否則模型改寫過的句子
會在兩欄同時出現、看起來完全吻合（理由詳見 evidence_lookup.py 開頭）。

搜尋是逐頁子字串比對（拿掉空白、異體字正規化後），不是向量檢索。6,387 頁全在
記憶體裡，一次查詢約數十毫秒，不需要索引。
"""
import difflib
import mimetypes
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402
paths.add_evidence_lookup_to_path()
import evidence_lookup as ev  # noqa: E402
import sources  # noqa: E402

# evidence_lookup 的路徑是相對 repo 根目錄寫的；改成絕對路徑，伺服器從哪裡啟動都對（外掛模式沒有 sample/）
ev.SRC_FALLBACKS = paths.IMAGE_FALLBACKS
ev.LOG = paths.VERDICTS

ROOT = paths.REPO                     # 外掛模式是 None
PAGES_JSONL = paths.PAGES_JSONL
# 舊題目存的範圍是 batch1／batch2，沿用這兩個鍵；其他分組用 g:<名稱>
LEGACY_SCOPES = {'第一批': 'batch1', '第二批': 'batch2'}
KIND_ORDER = {'naa': 0, 'pdf': 1, 'text': 2}
SNIPPET = 40

NAA_ID = re.compile(r'(\d{1,4})\s*/\s*image\s*0*(\d{1,5})')
NAA_IN_BRACKET = re.compile(r'(?:(\d{1,4})\s*/\s*)?image\s*0*(\d{1,5})')
DOC_ID = re.compile(r'([^【】「」/、,，;；\n]+?)\s*/\s*p\.?\s*0*(\d{1,5})(?!\d)')
DOC_IN_BRACKET = re.compile(r'(?:([^【】「」/、,，;；\n]+?)\s*/\s*)?(?<![A-Za-z])p\.?\s*0*(\d{1,5})(?!\d)')


def flat(text):
    """搜尋與比對用的正規化：去空白、統一 [?]、異體字換成通行字。"""
    return ev.squash(ev.norm_marks(text))[0].translate(ev.VARIANTS)


def doc_key(s):
    """模型抄文件代號常把底線寫成空白、多寫副檔名；比對時都拿掉。"""
    s = re.sub(r'\.(pdf|txt|md|markdown|epub)$', '', (s or '').strip(), flags=re.I)
    return re.sub(r'[\s_　]+', '', s).lower()


def ranges(positions):
    """[3,4,5,9] → [[3,6],[9,10]]"""
    out = []
    for p in sorted(positions):
        if out and out[-1][1] == p:
            out[-1][1] = p + 1
        else:
            out.append([p, p + 1])
    return out


class Corpus:
    def __init__(self, records=None):
        """records：sources.scan() 的紀錄。不給就讀 repo 的 data/delivery/pages.jsonl（測試用）。"""
        if records is None:
            records = sources.read_pages_jsonl(PAGES_JSONL)
        self.pages, self.vols = {}, {}
        for r in records:
            pid = r['page']
            if pid in self.pages:
                continue
            v = self.vols.setdefault(r['doc'], {
                'id': r['doc'], 'title': r.get('title') or r['doc'], 'folder': r.get('folder') or '',
                'batch': r['batch'], 'kind': r['kind'], 'file': r.get('file'), 'pages': [],
                'text_origin': r.get('text_origin') or 'unknown', 'note': r.get('doc_note'),
                'duplicate_of': r.get('duplicate_of')})
            v['pages'].append(pid)
            self.pages[pid] = {**r, 'flat': flat(r['text'])}
        for v in self.vols.values():
            v['pages'].sort(key=lambda p: (self.pages[p].get('index') or 0, p))
        self.doc_keys = {doc_key(d): d for d in self.vols}
        batches = dict.fromkeys(v['batch'] for v in self.ordered())
        self.scopes = {'all': '全部', **{LEGACY_SCOPES.get(b, f'g:{b}'): b for b in batches}}
        self._ref = None

    # ---------------------------------------------------------------- 基本資訊
    def ordered(self):
        return sorted(self.vols.values(), key=lambda v: (KIND_ORDER.get(v['kind'], 9), v['batch'],
                                                         int(v['id']) if v['id'].isdigit() else 0, v['id']))

    def summary(self):
        by_batch, kinds = {}, {}
        for v in self.vols.values():
            b = by_batch.setdefault(v['batch'], {'vols': 0, 'pages': 0})
            b['vols'] += 1
            b['pages'] += len(v['pages'])
            k = kinds.setdefault(v['kind'], {'vols': 0, 'pages': 0})
            k['vols'] += 1
            k['pages'] += len(v['pages'])
        return {'vols': len(self.vols), 'pages': len(self.pages), 'batches': by_batch, 'kinds': kinds,
                'naa': 'naa' in kinds, 'no_text': sum(1 for p in self.pages.values() if p.get('no_text'))}

    def in_scope(self, page, scope):
        return scope == 'all' or self.scopes.get(scope) == page['batch']

    def short(self, pid):
        """證據標籤上的短名：1017·136、報告書·p3。"""
        p = self.pages.get(pid)
        if p and p['kind'] == 'naa':
            return f"{p['doc']}·{int(pid.split('/image')[1])}"
        doc, _, n = pid.rpartition('/')
        return f"{doc if len(doc) <= 12 else doc[:11] + '…'}·{n}"

    def brief(self, pid):
        p = self.pages[pid]
        v = self.vols[p['doc']]
        # 不叫 kind：引文紀錄自己有 kind（quote／cite／ref），render 會把 brief 合併進去
        return {'page': pid, 'title': v['title'], 'batch': p['batch'], 'doc_kind': p['kind'], 'short': self.short(pid),
                'duplicate_of': v['duplicate_of']}

    def catalog(self, scope='all'):
        return [{'volume': v['id'], 'title': v['title'], 'batch': v['batch'], 'kind': v['kind'],
                 'text_origin': v['text_origin'], 'pages': len(v['pages']), 'first': v['pages'][0], 'last': v['pages'][-1],
                 **({'duplicate_scan_of': v['duplicate_of']} if v['duplicate_of'] else {}),
                 **({'note': v['note']} if v['note'] else {})}
                for v in self.ordered() if scope == 'all' or self.scopes.get(scope) == v['batch']]

    def neighbors(self, pid):
        pages = self.vols[self.pages[pid]['doc']]['pages']
        i = pages.index(pid)
        return (pages[i - 1] if i > 0 else None), (pages[i + 1] if i + 1 < len(pages) else None)

    def resolve_doc(self, raw):
        """文件代號 → 本專案的代號；前面多了以空白隔開的字（「見 zoning report」）就逐詞去掉再試。
        **只認空白斷開的前綴**：逐字去掉的話，寫錯的「新舊檔」會被對到「舊檔」，拿錯的文件核對引文。"""
        raw = (raw or '').strip()
        if raw in self.vols:
            return raw
        words = raw.split()
        for i in range(len(words)):
            hit = self.doc_keys.get(doc_key(' '.join(words[i:])))
            if hit:
                return hit
        return None

    def normalize_id(self, raw):
        """`【1017/image136】`、`1017/image00136`、`1017 / image00136.jpg` → `1017/image00136`；
        `報告書/p003`、`報告書.pdf / p3` → `報告書/p3`。"""
        m = NAA_ID.search(raw or '')
        if m:
            return f'{m.group(1)}/image{int(m.group(2)):05d}'
        m = DOC_ID.search(raw or '')
        if m:
            return f'{self.resolve_doc(m.group(1)) or m.group(1).strip()}/p{int(m.group(2))}'
        return None

    def parse_ids(self, inner):
        """【…】裡的頁碼；沒寫卷號（文件代號）的 image（p）沿用前一個。"""
        out, fonds = [], None
        for m in NAA_IN_BRACKET.finditer(inner):
            fonds = m.group(1) or fonds
            if fonds:
                out.append(f'{fonds}/image{int(m.group(2)):05d}')
        if not out:
            doc = None
            for m in DOC_IN_BRACKET.finditer(inner):
                if m.group(1):
                    doc = self.resolve_doc(m.group(1)) or m.group(1).strip()
                if doc:
                    out.append(f'{doc}/p{int(m.group(2))}')
        return list(dict.fromkeys(out))

    def ref_pattern(self):
        """正文裡沒加【】的頁碼（「1016/image00009 以後還沒讀」）的正規式片段。文件代號只認本專案有的，
        免得把一般文字誤認成頁碼。"""
        if self._ref is None:
            alts = [r'(?<![\d/])\d{1,4}/image\d{5}(?!\d)']
            docs = sorted((re.escape(d) for d, v in self.vols.items() if v['kind'] != 'naa'), key=len, reverse=True)
            if docs:
                alts.append(r'(?<![\w/])(?:' + '|'.join(docs) + r')/p\d{1,5}(?!\d)')
            self._ref = '|'.join(alts)
        return self._ref

    # ---------------------------------------------------------------- 搜尋與讀頁
    def search(self, query, scope='all', volume=None, limit=8):
        terms = [flat(t) for t in re.split(r'[\s,，、;；]+', query or '') if t.strip()]
        terms = [t for t in dict.fromkeys(terms) if t]
        if not terms:
            raise ValueError('沒有搜尋詞')
        doc = self.resolve_doc(str(volume)) if volume else None
        if volume and not doc:
            raise ValueError(f'沒有 {volume} 這一卷（文件）；請用 archive_catalog 列出的代號')
        scored = []
        for pid, p in self.pages.items():
            if doc and p['doc'] != doc:
                continue
            if not self.in_scope(p, scope):
                continue
            counts = [p['flat'].count(t) for t in terms]
            matched = sum(1 for c in counts if c)
            if matched:
                scored.append((-matched, -sum(counts), pid, counts))
        scored.sort()
        hits = []
        for _, _, pid, counts in scored[:limit]:
            p = self.pages[pid]
            best = min((t for t, c in zip(terms, counts) if c), key=lambda t: p['flat'].count(t))
            hits.append({**self.brief(pid), 'matched_terms': [t for t, c in zip(terms, counts) if c],
                         'snippet': self.snippet(pid, best)})
        return {'query': query, 'terms': terms, 'total': len(scored),
                'all_terms_pages': sum(1 for s in scored if -s[0] == len(terms)), 'hits': hits}

    def snippet(self, pid, term):
        text = self.pages[pid]['text']
        sq, pos = ev.squash(ev.norm_marks(text))
        i = sq.translate(ev.VARIANTS).find(term)
        if i < 0:
            return re.sub(r'\s+', ' ', text[:SNIPPET * 2])
        s, e = pos[i], pos[i + len(term) - 1] + 1
        clip = lambda x: re.sub(r'\s+', ' ', x.replace('@@PAGEBREAK', '〔換頁〕'))
        return (('…' if s > SNIPPET else '') + clip(text[max(0, s - SNIPPET):s]) + '〖' + clip(text[s:e]) + '〗'
                + clip(text[e:e + SNIPPET]) + ('…' if e + SNIPPET < len(text) else ''))

    def read(self, pid):
        p = self.pages[pid]
        prev, nxt = self.neighbors(pid)
        v = self.vols[p['doc']]
        out = {**self.brief(pid), 'prev': prev, 'next': nxt, 'text': p['text'], 'text_origin': p['text_origin']}
        if p.get('label'):
            out['label'] = p['label']
        if p['kind'] == 'naa':
            out.update(legibility=p.get('legible'), unread_marks=p.get('marks', 0))
        notes = []
        if p.get('no_text'):
            notes.append('這一頁沒有文字層（多半是掃描影像），讀不到字，不能引用；要先轉寫。')
        if out['duplicate_of']:
            notes.append(f"這份與 {out['duplicate_of']} 是同一份掃描，不可當成兩份獨立證據。")
        notes += [n.strip().rstrip('。') + '。' for n in re.split(r'[；;]', v['note'] or '') if n.strip()]
        if notes:
            out['note'] = ' '.join(notes)
        return out

    def origin(self, pid):
        """這頁的文字是怎麼來的（右欄「來源」用）。"""
        p = self.pages[pid]
        s = sources.ORIGINS.get(p['text_origin'], p['text_origin'])
        return f"{s}（{p['model']}）" if p.get('model') else s

    # ---------------------------------------------------------------- 引文比對
    def verify(self, quote, pid):
        """回傳 status（exact／variant／near／miss）、命中範圍、差異字的位置。"""
        text = self.pages[pid]['text']
        hit = ev.locate(quote, text)
        status = {'exact': 'exact', 'variant': 'variant', 'fuzzy': 'near'}.get(hit['match'], 'miss')
        out = {'status': status, 'score': hit.get('score')}
        if 'start' not in hit:
            return out
        s, e = hit['start'], hit['end']
        out.update(start=s, end=e)
        if status in ('variant', 'near'):
            q_sq, q_pos = ev.squash(ev.norm_marks(quote))
            t_sq, t_pos = ev.squash(ev.norm_marks(text[s:e]))
            src, said = set(), set()
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, q_sq, t_sq, autojunk=False).get_opcodes():
                if tag in ('replace', 'insert'):
                    src.update(s + t_pos[j] for j in range(j1, j2))
                if tag in ('replace', 'delete'):
                    said.update(q_pos[i] for i in range(i1, i2))
            out.update(src_marks=ranges(src), quote_marks=ranges(said))
        return out

    def find_quote(self, quote, candidates):
        """沒附出處的引文：在讀過的頁裡找逐字或異體字命中的那一頁。"""
        for pid in candidates:
            if pid in self.pages:
                v = self.verify(quote, pid)
                if v['status'] in ('exact', 'variant'):
                    return pid, v
        return None, None

    # ---------------------------------------------------------------- 原圖與原檔
    def image_path(self, pid):
        """頁面影像檔：文件資料夾的 images/、相容格式的原圖資料夾，都沒有再找 repo 的 sample/。"""
        p = self.pages[pid]
        if p.get('image_file') and pathlib.Path(p['image_file']).is_file():
            return pathlib.Path(p['image_file'])
        if p['kind'] == 'naa' and p.get('rel') and not p.get('pdf_file'):
            return ev.find_image(p['rel'])
        return None

    def has_image(self, pid):
        p = self.pages[pid]
        return bool(self.image_path(pid) or (p.get('pdf_file') and pathlib.Path(p['pdf_file']).is_file()))

    def image(self, pid, width):
        """回傳 (位元組, MIME)。沒有影像檔、原件是 PDF 的從 PDF 現畫；width=0 給原檔（PDF 畫成 2000px）。"""
        p = self.pages[pid]
        img = self.image_path(pid)
        if not img and p.get('pdf_file'):
            return sources.jpeg(sources.render_pdf_page(p['pdf_file'], p['pdf_page'], width or 2000)), 'image/jpeg'
        if not img:
            raise KeyError(pid)
        if not width:
            return img.read_bytes(), mimetypes.guess_type(img.name)[0] or 'image/jpeg'
        from PIL import Image
        with Image.open(img) as im:
            return sources.jpeg(im, width), 'image/jpeg'

    def original_file(self, pid):
        """「原檔」按鈕要開的檔：有影像檔開影像，否則開原件（PDF、文字檔）。"""
        p = self.pages[pid]
        img = self.image_path(pid)
        if img:
            return img
        f = pathlib.Path(p['file']) if p.get('file') else None
        return f if f and f.is_file() else None
