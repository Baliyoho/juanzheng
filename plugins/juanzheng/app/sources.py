"""卷證的 source 讀取：把研究專案 `source/` 資料夾裡的文件變成「一頁一筆」的標準紀錄，交給 corpus.Corpus。

**格式規格的正本是 docs/ai-notes/source-format.md**（2026-10-17 v1）；本檔是它的可執行形式，衝突時以規格為準。

- 文件資料夾：直接含 `pages.jsonl` 或 `original.*` 的資料夾＝一份文件（原件＋文字層＋選配的 images/）。
- 單檔文件：不在文件資料夾裡的 pdf／txt／md／epub，文字由程式抽。
- `_` 或 `.` 開頭的檔案與資料夾不讀；`source/_catalog.csv` 是文件層級總表（標題、文字來源、重複掃描、注意事項）。
- 相容格式（舊）：多卷 `pages.jsonl`、素材包「轉寫文字/」的 `@@PAGE` 單頁 txt、`<卷號 卷名>/imageNNNNN.jpg` 原圖。

頁碼兩種：`1017/image00136`（掃描影像一張）、`文件代號/p3`（PDF 第 3 頁或文字第 3 段）。
**掃描 PDF 多半沒有文字層**——檔案局給的原始 PDF 全是（2026-10-16 實測 0 字）：沒有轉寫就讀不到字、不能引用。
同一個頁碼出現兩次，先讀到的為準，另計重複數。
pdfium 不是執行緒安全的：伺服器是多執行緒，所有 PDF 操作都要拿 PDF_LOCK。
"""
import csv
import hashlib
import html.parser
import io
import json
import os
import pathlib
import posixpath
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

MIN_PARA = 200                      # 純文字：比這短的段落併入下一段
MAX_PARA = 2400                     # 純文字：比這長的段落在句號或換行處切開
TEXT_EXT = {'.txt', '.md', '.markdown'}
DOC_EXT = TEXT_EXT | {'.pdf', '.epub'}
IMG_EXT = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
NAA_ID = re.compile(r'^(\d{1,4})/image(\d{5})$')
PAGE_ID = re.compile(r'^(?:\d{1,4}/image\d{5}|[^/\s]+/p\d{1,5})$')     # 影像單位只用在數字卷號
CATALOG = '_catalog.csv'
PACK_DIRS = ('原始PDF', '逐頁JPG', '轉寫文字', '結果JSON')
PDF_LOCK = threading.Lock()
REPORT_LIST_MAX = 60                # 報告裡逐條列出的上限；超過只計數

# 文字來源：證據欄的警語、給模型的材料說明都依這個欄位（規格第四節）
ORIGINS = {
    'llm-transcription': '模型看影像轉寫（非人工校訂）',
    'ocr': 'OCR 辨識（非人工校訂）',
    'embedded': 'PDF 內嵌的文字層',
    'born-digital': '原生數位文字',
    'human': '人工繕打或校訂',
    'unknown': '來源不明的文字',
}


def reserved(name):
    """不讀進語料的名稱：. 開頭、_ 開頭（說明檔、總表）、Office 暫存 ~$。"""
    return name.startswith(('.', '_', '~$'))


def sha(text):
    return hashlib.sha1((text or '').encode('utf-8')).hexdigest()[:12]


def volume_title(fonds_name):
    """`1017 臺灣省…` → `臺灣省…`；第一批多一段檔號 `3 0061=2240201=1=3 國家安全…` 也去掉。"""
    parts = (fonds_name or '').split(' ')
    rest = parts[1:]
    if rest and re.fullmatch(r'[\d=\-_.]+', rest[0]):
        rest = rest[1:]
    return ' '.join(rest).strip() or fonds_name


def read_text(path):
    """回傳 (文字, 編碼)。UTF-8（含 BOM）優先；不是就用 charset_normalizer 猜，再不行用 Big5 硬讀。"""
    raw = pathlib.Path(path).read_bytes()
    try:
        return raw.decode('utf-8-sig'), 'utf-8'
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None:
            return str(best), best.encoding
    except ImportError:
        pass
    return raw.decode('cp950', errors='replace'), 'cp950?'


def doc_id(stem, taken):
    """檔名 → 文件代號：模型要照抄進【】，所以拿掉空白與會弄亂解析的符號，太長截斷，撞名加 ~2。"""
    s = re.sub(r'[\s　]+', '_', stem)
    s = re.sub(r'[/\\【】「」『』、,，;；#?%&]+', '', s).strip('_.') or 'doc'
    s = s[:40]
    out, n = s, 2
    while out in taken:
        out, n = f'{s}~{n}', n + 1
    taken.add(out)
    return out


def chunks(text):
    """純文字切段（規格第四節）：以空行分段落；`#` 標題與不到 MIN_PARA 字的段落併入下一段；
    超過 MAX_PARA 字的段落在換行或句號處切開。**不依累積字數切**：那樣前面改一個字，後面所有段號都會移位。"""
    paras = []
    for para in re.split(r'\n[ \t　]*\n', text.replace('\r\n', '\n').replace('\r', '\n')):
        para = para.strip('\n')
        if not para.strip():
            continue
        while len(para) > MAX_PARA:
            half = MAX_PARA // 2
            window = para[half:MAX_PARA]
            cut = max(window.rfind('\n'), window.rfind('。'))
            cut = half + cut + 1 if cut >= 0 else MAX_PARA
            paras.append(para[:cut].rstrip('\n'))
            para = para[cut:].lstrip('\n')
        if para.strip():
            paras.append(para)
    out, cur = [], ''
    for p in paras:
        cur = f'{cur}\n\n{p}' if cur else p
        if len(cur) >= MIN_PARA and not p.lstrip().startswith('#'):
            out.append(cur)
            cur = ''
    if cur:
        if out and len(cur) < MIN_PARA and not cur.lstrip().startswith('#'):
            out[-1] = f'{out[-1]}\n\n{cur}'          # 結尾的短段併回上一段，不要孤零零一段
        else:
            out.append(cur)
    return out or ['']


def unit_number(pid):
    """`1003/image00007` → 7、`報告/p3` → 3。"""
    m = re.search(r'(\d+)$', pid)
    return int(m.group(1)) if m else 0


def base(rec):
    """所有紀錄共用的欄位補齊：雜湊、文字來源、排序用的序號。"""
    rec.setdefault('index', unit_number(rec['page']))
    rec.setdefault('text_origin', 'unknown')
    rec['sha'] = sha(rec['text'])
    return rec


# ---------------------------------------------------------------- 文字層：pages.jsonl

def is_pages_jsonl(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    r = json.loads(line)
                except ValueError:
                    return False
                return isinstance(r, dict) and 'page_id' in r and 'text' in r
    return False


def table_rows(path, naa_only=False):
    """標準頁紀錄（規格第四節）。失敗頁（error）、空白頁、頁碼不合格式的不收。"""
    pat = NAA_ID if naa_only else PAGE_ID
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get('error') or not r.get('text') or not pat.match(r.get('page_id') or ''):
                continue
            yield r


def row_record(r, batch):
    origin = r.get('text_origin') or ('llm-transcription' if r.get('model') else 'unknown')
    fonds = r['page_id'].split('/', 1)[0]
    return {'page': r['page_id'], 'doc': fonds, 'kind': 'naa' if '/image' in r['page_id'] else 'text',
            'text': r['text'], 'batch': r.get('batch') or batch or 'pages.jsonl',
            'title': volume_title(r.get('fonds_name') or fonds), 'folder': r.get('fonds_name') or fonds,
            'cid': r.get('cid'), 'rel': r.get('src_rel'), 'legible': r.get('legible'), 'marks': r.get('marks', 0),
            'model': r.get('model'), 'text_origin': origin, 'label': r.get('label'), 'src_page': r.get('src_page')}


def read_pages_jsonl(path, batch=None):
    """相容格式：不在文件資料夾裡的多卷轉寫總表（素材包、data/delivery 的那份）。只收檔案局頁碼。"""
    for r in table_rows(path, naa_only=True):
        yield base(row_record(r, batch))


def page_txt_record(text, batch):
    """相容格式：素材包「轉寫文字/」的單頁檔，開頭幾行 @@KEY 值，空一行後是正文。不是這種格式回 None。"""
    lines = text.replace('\r\n', '\n').split('\n')
    meta, i = {}, 0
    while i < len(lines) and lines[i].startswith('@@'):
        k, _, v = lines[i][2:].partition(' ')
        meta[k] = v.strip()
        i += 1
    pid = meta.get('PAGE', '')
    if not NAA_ID.match(pid):
        return None
    ocr = dict(kv.split('=', 1) for kv in meta.get('OCR', '').split() if '=' in kv)
    fonds = pid.split('/', 1)[0]
    return base({'page': pid, 'doc': fonds, 'kind': 'naa', 'text': '\n'.join(lines[i:]).strip('\n'), 'batch': batch,
                 'title': fonds, 'folder': fonds, 'cid': meta.get('CID'), 'rel': meta.get('SRC'),
                 'legible': ocr.get('legible'), 'marks': int(ocr.get('marks') or 0), 'model': ocr.get('model'),
                 'text_origin': 'llm-transcription' if ocr.get('model') else 'unknown'})


# ---------------------------------------------------------------- 原件：txt／md、PDF、epub

def text_records(path, rel, text, doc, batch, labels=None):
    return [base({'page': f'{doc}/p{i}', 'doc': doc, 'kind': 'text', 'text': t, 'batch': batch, 'title': rel,
                  'folder': rel, 'file': str(path), 'index': i, 'text_origin': 'born-digital',
                  'label': labels[i - 1] if labels else None})
            for i, t in enumerate(chunks(text) if labels is None else text, 1)]


def pdf_texts(path, cache_dir):
    """PDF 每頁的文字層與印刷頁碼（page label）。抽過的依（大小、修改時間）快取在 cache_dir。"""
    import pypdfium2 as pdfium
    path = pathlib.Path(path)
    st = path.stat()
    cf = None
    if cache_dir:
        cf = pathlib.Path(cache_dir) / f"pdf-{hashlib.sha1(str(path.resolve()).encode('utf-8')).hexdigest()[:16]}.json"
        try:
            c = json.loads(cf.read_text(encoding='utf-8'))
            if c['size'] == st.st_size and c['mtime'] == st.st_mtime_ns:
                return c['pages'], c['labels']
        except (OSError, ValueError, KeyError):
            pass
    with PDF_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            pages, labels = [], []
            for i in range(len(pdf)):
                page = pdf[i]
                tp = page.get_textpage()
                pages.append(tp.get_text_bounded().replace('\r\n', '\n').replace('\r', '\n'))
                tp.close()
                page.close()
                try:
                    labels.append(pdf.get_page_label(i) or None)
                except Exception:
                    labels.append(None)
        finally:
            pdf.close()
    if cf:
        cf.parent.mkdir(parents=True, exist_ok=True)
        tmp = cf.with_suffix('.tmp')
        tmp.write_text(json.dumps({'size': st.st_size, 'mtime': st.st_mtime_ns, 'pages': pages, 'labels': labels},
                                  ensure_ascii=False), encoding='utf-8')
        tmp.replace(cf)
    return pages, labels


def pdf_page_count(path):
    import pypdfium2 as pdfium
    with PDF_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()


def pdf_records(path, rel, doc, batch, cache_dir):
    out = []
    pages, labels = pdf_texts(path, cache_dir)
    for i, (t, label) in enumerate(zip(pages, labels), 1):
        no_text = len(re.sub(r'\s', '', t)) < 5
        out.append(base({'page': f'{doc}/p{i}', 'doc': doc, 'kind': 'pdf', 'text': '' if no_text else t.strip('\n'),
                         'batch': batch, 'title': rel, 'folder': rel, 'file': str(path), 'index': i,
                         'pdf_file': str(path), 'pdf_page': i, 'no_text': no_text, 'text_origin': 'embedded',
                         'label': label if label and label != str(i) else None}))
    return out


class _HtmlText(html.parser.HTMLParser):
    """epub 的 XHTML → 純文字：區塊元素之間空一行，標題前加 #（chunks 會把它併進下一段）。"""
    BLOCK = {'p', 'div', 'section', 'article', 'blockquote', 'li', 'tr', 'table', 'ul', 'ol', 'pre',
             'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
    SKIP = {'script', 'style', 'head', 'title'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.heading = [], 0, None
        self.first_heading = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'br':
            self.out.append('\n')
        elif tag in self.BLOCK:
            self.out.append('\n\n')
            if re.fullmatch(r'h[1-6]', tag):
                self.out.append('# ')
                self.heading = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            if re.fullmatch(r'h[1-6]', tag) and self.heading is not None:
                if self.first_heading is None:
                    self.first_heading = ''.join(self.heading).strip() or None
                self.heading = None
            self.out.append('\n\n')

    def handle_data(self, data):
        if not self.skip:
            data = re.sub(r'[ \t\r\n]+', ' ', data)
            self.out.append(data)
            if self.heading is not None:
                self.heading.append(data)

    def text(self):
        s = ''.join(self.out)
        s = re.sub(r'[ \t]*\n[ \t]*', '\n', s)
        return re.sub(r'\n{3,}', '\n\n', s).strip()


def epub_sections(path):
    """依 spine 順序回傳 [(章名或 None, 純文字)]。只用標準函式庫（zip＋XML）。"""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        rootfile = container.find('.//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')
        opf_path = rootfile.get('full-path')
        opf = ET.fromstring(z.read(opf_path))
        ns = {'o': 'http://www.idpf.org/2007/opf'}
        folder = posixpath.dirname(opf_path)
        items = {i.get('id'): i for i in opf.iterfind('.//o:manifest/o:item', ns)}
        out = []
        for ref in opf.iterfind('.//o:spine/o:itemref', ns):
            item = items.get(ref.get('idref'))
            if item is None or 'html' not in (item.get('media-type') or ''):
                continue
            href = posixpath.normpath(posixpath.join(folder, urllib.parse.unquote(item.get('href') or '')))
            p = _HtmlText()
            p.feed(z.read(href).decode('utf-8', errors='replace'))
            text = p.text()
            if text:
                out.append((p.first_heading, text))
        return out


def epub_records(path, rel, doc, batch):
    """epub：全書段落連續編號 pN，章名放 label。"""
    texts, labels = [], []
    for heading, text in epub_sections(path):
        for t in chunks(text):
            texts.append(t)
            labels.append(heading)
    return text_records(path, rel, texts or [''], doc, batch, labels=labels or [None])


def original_records(f, rel, doc, batch, cache_dir, rep):
    """單檔文件或文件資料夾的原件 → 紀錄。不支援的格式回 None。"""
    ext = f.suffix.lower()
    if ext == '.pdf':
        return pdf_records(f, rel, doc, batch, cache_dir)
    if ext == '.epub':
        return epub_records(f, rel, doc, batch)
    if ext in TEXT_EXT:
        text, enc = read_text(f)
        if enc != 'utf-8':
            rep['encodings'][rel] = enc
        return text_records(f, rel, text, doc, batch)
    return None


def render_pdf_page(path, index, width):
    """PDF 第 index 頁（從 1 起算）畫成 PIL 影像，寬約 width 像素。"""
    import pypdfium2 as pdfium
    with PDF_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            page = pdf[index - 1]
            scale = max(.3, min(width / max(page.get_width(), 1), 8))
            im = page.render(scale=scale).to_pil()
            page.close()
        finally:
            pdf.close()
    return im


def image_key(path):
    """相容格式：`…/1003 卷名/image00007.jpg` → (`1003/image00007`, `1003 卷名`)；對不上回 (None, None)。"""
    m = re.fullmatch(r'image\s*0*(\d{1,5})', path.stem, re.I)
    f = re.match(r'(\d{1,4})(?:[\s_]|$)', path.parent.name)
    if not (m and f):
        return None, None
    return f'{f.group(1)}/image{int(m.group(1)):05d}', path.parent.name


# ---------------------------------------------------------------- 文件資料夾

def split_name(name):
    """資料夾名 `1003 獎勵投資條例…` → (`1003`, `獎勵投資條例…`)；沒有空白 → (名稱, None)。"""
    m = re.match(r'(\S+)[\s　]+(.+)$', name.strip())
    return (m.group(1), m.group(2).strip()) if m else (name.strip(), None)


def is_doc_dir(d):
    try:
        return any(not f.is_dir() and (f.name.lower() == 'pages.jsonl' or f.stem.lower() == 'original')
                   for f in d.iterdir())
    except OSError:
        return False


def doc_folder_records(d, rel, batch, taken, cache_dir, rep, files):
    """一個文件資料夾 → 紀錄（規格第三節）。files：這個資料夾底下（已排除保留名稱）的所有檔。"""
    warn = rep['warnings'].append
    table = d / 'pages.jsonl' if (d / 'pages.jsonl').is_file() else None
    originals = sorted((f for f in files if f.parent == d and f.stem.lower() == 'original'),
                       key=lambda f: (f.suffix.lower() != '.pdf', f.name))
    original = originals[0] if originals else None
    if len(originals) > 1:
        warn(f'{rel}：有 {len(originals)} 個 original.*，只用 {original.name}')
    images = {f.stem.lower(): f for f in files if f.parent == d / 'images' and f.suffix.lower() in IMG_EXT}
    known = {table, *originals, *images.values()}
    for f in files:
        if f not in known:
            rep['unknown'] += 1
            if len(rep['skipped']) < REPORT_LIST_MAX:
                rep['skipped'].append({'rel': f'{rel}/{f.relative_to(d).as_posix()}',
                                       'why': '文件資料夾裡不認得的檔（要放 original.*、pages.jsonl 或 images/）'})
    name_id, name_title = split_name(d.name)
    title = volume_title(d.name) if name_title else d.name
    if table:
        recs = []
        for r in table_rows(table):
            rec = row_record(r, batch)
            rec.update(title=title, folder=rel, batch=batch or r.get('batch'))
            unit = rec['page'].split('/', 1)[1].lower()
            if original:
                rec['file'] = str(original)
                if original.suffix.lower() == '.pdf':
                    rec['kind'] = rec['kind'] if rec['kind'] == 'naa' else 'pdf'
                    rec['pdf_file'], rec['pdf_page'] = str(original), int(r.get('src_page') or unit_number(unit))
            if unit in images:
                rec['image_file'] = str(images[unit])
            recs.append(base(rec))
        docs = sorted({r['doc'] for r in recs})
        if len(docs) > 1:
            warn(f"{rel}：pages.jsonl 混了 {len(docs)} 份文件的頁（{'、'.join(docs[:5])}）；一個文件資料夾只放一份")
        elif docs and docs[0] != name_id:
            warn(f'{rel}：資料夾名的代號「{name_id}」與頁碼前綴「{docs[0]}」不同，以頁碼為準')
        taken.update(docs)
        if not recs:
            warn(f'{rel}：pages.jsonl 沒有可用的頁（要有 page_id 與 text）')
        return recs
    doc = doc_id(name_id, taken)
    recs = original_records(original, rel, doc, batch, cache_dir, rep)
    if recs is None:
        warn(f'{rel}：不支援 {original.name} 的格式')
        return []
    for r in recs:
        r.update(title=name_title or d.name, folder=rel)
    return recs


# ---------------------------------------------------------------- 文件層級總表

def read_catalog(source):
    """`_catalog.csv` → {代號: {title, text_origin, duplicate_of, note}}。欄名不分大小寫，空值不覆寫。"""
    f = pathlib.Path(source) / CATALOG
    if not f.is_file():
        return {}
    text = read_text(f)[0]
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or '').strip().lower(): (v or '').strip() for k, v in row.items()}
        if row.get('doc'):
            out[row['doc']] = {k: row[k] for k in ('title', 'text_origin', 'duplicate_of', 'note') if row.get(k)}
    return out


def write_catalog(source, entries):
    """合併寫入 `_catalog.csv`：已經有的列（使用者可能改過）保留，只補新的代號。回傳新增幾列。"""
    f = pathlib.Path(source) / CATALOG
    old = read_catalog(source)
    rows = [{'doc': d, **v} for d, v in old.items()]
    added = 0
    for d, v in entries.items():
        if d not in old:
            rows.append({'doc': d, **v})
            added += 1
    rows.sort(key=lambda r: (not r['doc'].isdigit(), int(r['doc']) if r['doc'].isdigit() else 0, r['doc']))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=['doc', 'title', 'text_origin', 'duplicate_of', 'note'], lineterminator='\n')
    w.writeheader()
    w.writerows(rows)
    tmp = f.with_suffix('.tmp')
    tmp.write_text(buf.getvalue(), encoding='utf-8-sig')       # 有 BOM，Excel 開才不會亂碼
    tmp.replace(f)
    return added


def apply_catalog(records, catalog, rep):
    docs = {r['doc'] for r in records}
    for r in records:
        c = catalog.get(r['doc'])
        if not c:
            continue
        if c.get('title'):
            r['title'] = c['title']
        if c.get('text_origin') in ORIGINS:
            r['text_origin'] = c['text_origin']
        r['doc_note'] = c.get('note')
        r['duplicate_of'] = c.get('duplicate_of')
    missing = sorted(d for d in catalog if d not in docs)
    if missing:
        rep['warnings'].append(f"{CATALOG} 有 {len(missing)} 個代號在 source 裡找不到：{'、'.join(missing[:8])}"
                               + ('…' if len(missing) > 8 else ''))
    bad = sorted({c['text_origin'] for c in catalog.values() if c.get('text_origin') and c['text_origin'] not in ORIGINS})
    if bad:
        rep['warnings'].append(f"{CATALOG} 的 text_origin 不認得：{'、'.join(bad)}（可用：{'、'.join(ORIGINS)}）")


# ---------------------------------------------------------------- 掃描整個 source/

def walk(root):
    """source 底下所有檔案（略過保留名稱的檔案與資料夾），依路徑排序。"""
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not reserved(x))
        out += [pathlib.Path(d) / f for f in files if not reserved(f)]
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


def scan(source, cache_dir=None):
    """回傳 (紀錄清單, 報告)。報告給畫面看：讀了什麼、略過什麼、哪些頁讀不到字、有什麼要提醒。"""
    source = pathlib.Path(source)
    files = walk(source) if source.is_dir() else []
    records, seen, taken = [], set(), set()
    images, folders = {}, {}
    rep = {'files': 0, 'docs': 0, 'skipped': [], 'skipped_total': 0, 'unknown': 0, 'duplicates': 0, 'images': 0,
           'no_text': [], 'no_text_pages': 0, 'encodings': {}, 'warnings': [], 'pack_layout': None}

    def skip(rel, why):
        rep['skipped_total'] += 1
        if len(rep['skipped']) < REPORT_LIST_MAX:
            rep['skipped'].append({'rel': rel, 'why': why})

    # 文件資料夾：直接含 pages.jsonl 或 original.* 的資料夾（取最外層），整個資料夾是一個單位
    # source 本身不算（舊專案根目錄放的多卷 pages.jsonl）；素材包原樣（有 原始PDF／轉寫文字… 子資料夾）也不算
    packs = {d for d in {f.parent.parent for f in files} | {source}
             if d.is_dir() and sum((d / x).is_dir() for x in PACK_DIRS) >= 3}
    rep['pack_layout'] = sorted(d.relative_to(source).as_posix() or '.' for d in packs) or None
    doc_dirs = sorted({f.parent for f in files if (f.name.lower() == 'pages.jsonl' or f.stem.lower() == 'original')
                       and f.parent != source and f.parent not in packs}, key=lambda d: len(d.parts))
    outer = []
    for d in doc_dirs:
        if not any(o in d.parents for o in outer):
            outer.append(d)
    owner = {}
    for f in files:
        o = next((d for d in outer if d in f.parents), None)
        if o:
            owner.setdefault(o, []).append(f)
    units = [('doc', d) for d in outer] + [('file', f) for f in files if not any(d in f.parents for d in outer)]
    units.sort(key=lambda u: u[1].relative_to(source).as_posix())

    for kind, f in units:                             # 相容格式的原圖索引，轉寫頁才找得到圖
        if kind == 'file' and f.suffix.lower() in IMG_EXT:
            key, folder = image_key(f)
            if key:
                images.setdefault(key, f)
                folders.setdefault(key.split('/')[0], folder)
    for kind, f in units:
        rel = f.relative_to(source).as_posix()
        group = rel.split('/', 1)[0] if '/' in rel else None
        try:
            if kind == 'doc':
                recs = doc_folder_records(f, rel, group, taken, cache_dir, rep, owner[f])
                if recs:
                    rep['docs'] += 1
                    for r in recs:
                        r['batch'] = r['batch'] or '文件'
            else:
                ext, batch = f.suffix.lower(), group or '文件'
                if ext == '.jsonl':
                    if not is_pages_jsonl(f):
                        skip(rel, '不認得的 JSONL 格式（要有 page_id 與 text 欄位）')
                        continue
                    recs = list(read_pages_jsonl(f, batch))
                elif ext == '.txt' and (head := read_text(f)[0]).startswith('@@PAGE'):
                    recs = [r for r in [page_txt_record(head, batch)] if r]
                elif ext in DOC_EXT:
                    recs = original_records(f, rel, doc_id(f.stem, taken), batch, cache_dir, rep)
                elif ext in IMG_EXT:
                    if not image_key(f)[0]:
                        skip(rel, '影像對不上頁碼（要放在文件資料夾的 images/，或「卷號 卷名」資料夾、檔名 imageNNNNN）')
                    continue
                else:
                    skip(rel, '不支援的格式')
                    continue
        except Exception as ex:                       # 壞檔、加密 PDF…：列出來，不讓整個專案打不開
            skip(rel, f'讀不了：{type(ex).__name__} {ex}'[:160])
            continue
        rep['files'] += 1
        for r in recs:
            if r['page'] in seen:
                rep['duplicates'] += 1
                continue
            seen.add(r['page'])
            if r['kind'] == 'naa' and not r.get('image_file') and not r.get('pdf_file'):
                img = images.get(r['page'])
                if img:
                    r['image_file'] = str(img)
                if r['title'] == r['doc'] and r['doc'] in folders:
                    r['title'], r['folder'] = volume_title(folders[r['doc']]), folders[r['doc']]
            if r['kind'] == 'naa' and (r.get('image_file') or r.get('pdf_file')):
                rep['images'] += 1
            if r.get('no_text'):
                rep['no_text_pages'] += 1
                if r['doc'] not in rep['no_text'] and len(rep['no_text']) < REPORT_LIST_MAX:
                    rep['no_text'].append(r['doc'])
            records.append(r)
    rep['skipped_total'] += rep['unknown']
    apply_catalog(records, read_catalog(source), rep)
    return records, rep


def jpeg(im, width=None, quality=86):
    """PIL 影像 → JPEG 位元組；比 width 寬就等比例縮小。"""
    from PIL import Image
    im = im.convert('RGB')
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=quality, optimize=True)
    return buf.getvalue()
