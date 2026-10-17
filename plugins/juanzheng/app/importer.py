"""把舊格式的材料轉成 source 格式規格 v1（docs/ai-notes/source-format.md）的文件資料夾。

- `import_pack()`：素材包（原始PDF／逐頁JPG／轉寫文字／結果JSON＋pages.jsonl＋manifest.csv）→ 每卷一個
  `<卷號 卷名>/`（`original.pdf`＋該卷的 `pages.jsonl`），寫 `_catalog.csv`，讀我與統計檔改名成底線開頭帶進來。
  逐頁 JPG 預設不複製（卷證從 PDF 現畫，省 4.5 GB）；結果 JSON 不複製（來歷欄位已在 pages.jsonl）。
- `split_table()`：多卷的 `pages.jsonl`（`data/delivery/` 那份）依卷拆成文件資料夾；沒有原件，
  原圖照舊從 repo 的 `sample/` 找。舊題目搬家用。

**原則**：來源一律只讀不改；目的地已經有的文件資料夾不覆蓋；`_catalog.csv` 已有的列（使用者可能改過）保留。
**PDF 頁數、manifest 頁數、轉寫頁數三者對不上的卷整卷不匯入並回報**——對錯頁比少一卷糟得多：
引文會指向別頁的原圖，而畫面上看不出來。
"""
import csv
import io
import json
import pathlib
import re
import shutil

import sources

# 第二批（卷號 1001–1046）已知的文件層級事實。出處：docs/ai-notes/batch2-corpus.md、素材包讀我第四節。
# 這是**一次性轉換**用的：匯入時寫進專案的 _catalog.csv，之後卷證只讀 _catalog.csv，不再看這裡。
KNOWN_NOTES = (
    (range(1001, 1047), '卷號是合成編號（這批 PDF 沒有檔案局的全宗號），不是檔案局的卷號'),
    (range(1038, 1047), '原圖僅約 1000×1500px，內容是密排直式小字，轉寫錯字較多，引用前務必對原圖'),
)
KNOWN_DUPLICATES = (('1038', '1039'), ('1042', '1043'), ('1045', '1046'))


def safe_name(name):
    """Windows 資料夾名不能有的字元換成底線，尾端不能是點或空白。"""
    return re.sub(r'[\\/:*?"<>|]', '_', name).rstrip(' .') or '_'


def known_catalog(docs):
    """依 KNOWN_NOTES／KNOWN_DUPLICATES 產生 _catalog.csv 的列（只給 docs 裡有的代號）。"""
    out = {}
    for d in docs:
        notes = [n for r, n in KNOWN_NOTES if d.isdigit() and int(d) in r]
        dup = next((b if d == a else a for a, b in KNOWN_DUPLICATES if d in (a, b)), None)
        if notes or dup:
            out[d] = {k: v for k, v in (('note', '；'.join(notes)), ('duplicate_of', dup if dup in docs else None)) if v}
    return out


def write_rows(path, rows):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    tmp.replace(path)


def standard_row(r):
    """素材包的 27 欄頁紀錄 → 標準頁紀錄：原欄位照留，補 text_origin 與 src_page。"""
    r = dict(r)
    r.setdefault('text_origin', 'llm-transcription' if r.get('model') else 'unknown')
    if r.get('page_no') and 'src_page' not in r:
        r['src_page'] = r['page_no']
    return r


def group_rows(table):
    """多卷 pages.jsonl → {卷號: [列…]}（依 page_no／頁碼排序）。失敗頁、空白頁不收。"""
    vols = {}
    for r in sources.table_rows(table, naa_only=True):
        vols.setdefault(r['page_id'].split('/', 1)[0], []).append(r)
    for rows in vols.values():
        rows.sort(key=lambda r: (r.get('page_no') or sources.unit_number(r['page_id'])))
    return vols


def split_table(table, source):
    """多卷 pages.jsonl → source 底下每卷一個文件資料夾（沒有原件）。回傳 {made, kept, catalog}。"""
    source = pathlib.Path(source)
    made, kept = [], []
    vols = group_rows(table)
    for fonds, rows in vols.items():
        d = source / safe_name(rows[0].get('fonds_name') or fonds)
        if d.exists():
            kept.append(d.name)
            continue
        d.mkdir(parents=True)
        write_rows(d / 'pages.jsonl', [standard_row(r) for r in rows])
        made.append(d.name)
    added = sources.write_catalog(source, known_catalog(set(vols)))
    return {'made': len(made), 'kept': kept, 'catalog': added}


def read_manifest(pack):
    f = pathlib.Path(pack) / 'manifest.csv'
    return [{k.strip(): (v or '').strip() for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(sources.read_text(f)[0]))]


def check_pack(pack):
    """素材包齊不齊；不齊回傳缺什麼（給畫面看的一句話），齊了回 None。"""
    pack = pathlib.Path(pack)
    need = [x for x in ('pages.jsonl', 'manifest.csv', '原始PDF') if not (pack / x).exists()]
    if need:
        return f"這不是完整的素材包（缺 {'、'.join(need)}）：{pack}"
    return None


def find_file(folder, name):
    """manifest 寫的副檔名大小寫可能和實際檔名不同（.PDF／.pdf）。"""
    f = folder / name
    if f.is_file():
        return f
    low = name.lower()
    return next((x for x in folder.iterdir() if x.name.lower() == low), None) if folder.is_dir() else None


def import_pack(pack, source, copy_images=False, log=print):
    """素材包 → source 的文件資料夾。回傳摘要：made（新建的卷）、kept（已存在、沒動）、problems（整卷沒匯入的原因）。"""
    pack, source = pathlib.Path(pack), pathlib.Path(source)
    err = check_pack(pack)
    if err:
        raise ValueError(err)
    vols = group_rows(pack / 'pages.jsonl')
    made, kept, problems, copied_images, docs = [], [], [], 0, set()
    for m in read_manifest(pack):
        fonds, folder = m.get('卷號', ''), m.get('目錄名') or ''
        rows = vols.get(fonds)
        if not rows:
            problems.append(f'{fonds}：pages.jsonl 裡沒有這一卷的頁')
            continue
        d = source / safe_name(folder or rows[0].get('fonds_name') or fonds)
        if d.exists():
            kept.append(d.name)
            docs.add(fonds)
            continue
        pdf = find_file(pack / '原始PDF', m.get('原檔名', ''))
        if not pdf:
            problems.append(f"{fonds}：原始PDF 裡找不到 {m.get('原檔名')}")
            continue
        n_pdf, n_manifest = sources.pdf_page_count(pdf), int(m.get('頁數') or 0)
        n_rows = max(r.get('page_no') or sources.unit_number(r['page_id']) for r in rows)
        if not (n_pdf == n_manifest and n_rows <= n_pdf):
            problems.append(f'{fonds}：頁數對不上（PDF {n_pdf}、manifest {n_manifest}、轉寫最後一頁 {n_rows}），整卷沒匯入')
            continue
        # 先寫到底線開頭的暫存資料夾（卷證不讀），整卷寫完才改名：中途失敗（磁碟滿、拔隨身碟）不會留下半卷，
        # 下次匯入也不會因為「資料夾已存在」而跳過它
        tmp = source / f'_匯入中-{d.name}'
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        log(f'匯入 {d.name}（{n_pdf} 頁）')
        shutil.copy2(pdf, tmp / ('original' + pdf.suffix.lower()))
        write_rows(tmp / 'pages.jsonl', [standard_row(r) for r in rows])
        jpg = pack / '逐頁JPG' / folder
        if copy_images and jpg.is_dir():
            (tmp / 'images').mkdir()
            for f in sorted(jpg.iterdir()):
                if f.suffix.lower() in sources.IMG_EXT:
                    shutil.copy2(f, tmp / 'images' / f.name)
                    copied_images += 1
        tmp.rename(d)
        made.append(d.name)
        docs.add(fonds)
    added = sources.write_catalog(source, known_catalog(docs))
    for name, dest in (('讀我.md', '_素材包讀我.md'), ('pages.meta.json', '_素材包統計.json'), ('manifest.csv', '_素材包manifest.csv')):
        if (pack / name).is_file() and not (source / dest).exists():
            shutil.copy2(pack / name, source / dest)
    return {'made': len(made), 'kept': kept, 'problems': problems, 'catalog': added, 'images': copied_images}
