"""把模型的回答（Markdown 子集）拆成結構化段落，並逐條核對引文。

前端只負責畫，不再自己解析——引文編號、比對結果、哪段字要標色都在這裡定案，
前後端不會對同一段文字解析出不同結果。

回答格式由 agent.py 的系統提示規定：逐字引文放「」，緊接【卷號/imageNNNNN】或【文件代號/pN】
（頁碼怎麼解析在 corpus.parse_ids／normalize_id，這裡不另寫一套）。
實務上模型不一定守規矩，所以這裡也處理三種走樣：
- 「」後面沒有【】：在本題目讀過的頁裡自動找出處（找到標 auto，找不到標 uncited）；
  太短的「」多半是名詞（「工業區」），不當引文。
- 一個【】裡塞好幾頁（【1017/image00136、image00137】）：拆開，每頁一條。
- 引用的頁不在語料裡：標 nopage。
- 正文裡沒加【】的頁碼（「限制」段常寫「1016/image00009 以後還沒讀」）：做成可點的 ref，
  不算進引文核對。
"""
import re

# 引文＋緊接的出處｜單獨的【】｜正文裡沒加【】的頁碼（最後這種依專案有哪些文件而定，見 inline_re）
QUOTE_OR_BRACKET = (r'「(?P<quote>[^「」\n]{1,400})」(?P<tail>(?:[ \t]*【[^】\n]{3,160}】)*)'
                    r'|【(?P<br>[^】\n]{3,160})】')
BRACKET = re.compile(r'【([^】\n]{3,160})】')
MIN_QUOTE = 6           # 未附出處的「」少於這個字數，視為名詞而不是引文
LIST_ITEM = re.compile(r'^\s*(?:[-*•]|\d+[.、)）])\s+')
ORDERED = re.compile(r'^\s*\d+[.、)）]\s+')


def inline_re(corpus):
    return re.compile(f'{QUOTE_OR_BRACKET}|(?P<ref>{corpus.ref_pattern()})')


class Annotator:
    def __init__(self, corpus, read_pages):
        self.corpus = corpus
        self.read_pages = set(read_pages)
        self.cites = []
        self.pattern = inline_re(corpus)

    def add(self, **c):
        c['n'] = len(self.cites)
        page = c.get('page')
        c['read'] = bool(page and page in self.read_pages)
        if page and page not in self.corpus.pages:
            c['status'] = 'nopage'
        elif page:
            c.update(self.corpus.brief(page))
            c['sha'] = self.corpus.pages[page]['sha']      # 重開題目時用來發現原文變了（規格第六節）
        self.cites.append(c)
        return c['n']

    def quote(self, text, pages):
        """一段「」引文＋它標的頁。回傳 inline 片段。"""
        squashed = re.sub(r'\s', '', text)
        if not pages:
            # 名詞（「工業區」）或一串搜尋詞（「糾正案、函復、改善」）不是史料引文
            # 只認頓號且至少三個詞：「水源充足,土地肥沃」是真引文，不能誤排除
            terms = squashed.split('、')
            if len(squashed) < MIN_QUOTE or (len(terms) >= 3 and max(map(len, terms)) <= 4):
                return [f'「{text}」']
            page, v = self.corpus.find_quote(text, sorted(self.read_pages))
            if page:
                return [{'q': self.add(kind='quote', quote=text, page=page, auto=True, **v)}]
            return [{'q': self.add(kind='quote', quote=text, page=None, status='uncited')}]
        known = [p for p in pages if p in self.corpus.pages]
        results = {p: self.corpus.verify(text, p) for p in known}
        rank = {'exact': 0, 'variant': 1, 'near': 2, 'miss': 3}
        best = min(known, key=lambda p: (rank[results[p]['status']], -(results[p].get('score') or 0)),
                   default=None)
        parts = []
        if best:
            parts.append({'q': self.add(kind='quote', quote=text, page=best, **results[best])})
        else:
            parts.append({'q': self.add(kind='quote', quote=text, page=pages[0])})
        for p in pages:
            if p != (best or pages[0]):
                parts.append({'c': self.add(kind='cite', page=p, status='cite')})
        return parts

    def cite(self, pages):
        return [{'c': self.add(kind='cite', page=p, status='cite')} for p in pages]

    def inline(self, text):
        parts, last = [], 0
        for m in self.pattern.finditer(text):
            if m.start() > last:
                parts.append(text[last:m.start()])
            if m.group('quote') is not None:
                pages = []
                for inner in BRACKET.findall(m.group('tail') or ''):
                    pages += self.corpus.parse_ids(inner)
                parts += self.quote(m.group('quote'), list(dict.fromkeys(pages)))
            elif m.group('br') is not None:
                pages = self.corpus.parse_ids(m.group('br'))
                parts += self.cite(pages) if pages else [m.group(0)]
            else:
                pid = self.corpus.normalize_id(m.group('ref')) or m.group('ref')
                parts.append({'c': self.add(kind='ref', page=pid, status='ref')})
            last = m.end()
        if last < len(text):
            parts.append(text[last:])
        return parts


def finalize(cites):
    """引用沒讀過的頁：cite 類改標 unread；quote 類保留比對結果，另由 read=false 呈現。"""
    for c in cites:
        if c['kind'] == 'cite' and c.get('status') == 'cite' and not c['read']:
            c['status'] = 'unread'
    return cites


def parse(answer, corpus, read_pages):
    """回傳 (blocks, citations)。blocks 每項是 [類型, ...]：
    ['h', level, inlines] ['p', inlines] ['ul'|'ol', [inlines…]] ['table', [[inlines…]…]]
    ['bq', inlines] ['hr']。inlines 是字串或 {'q': n}／{'c': n}。"""
    ann = Annotator(corpus, read_pages)
    blocks, para = [], []
    lines = (answer or '').replace('\r\n', '\n').split('\n')

    def flush():
        if para:
            blocks.append(['p', ann.inline(' '.join(s.strip() for s in para))])
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
        elif re.fullmatch(r'[-*_]{3,}', s):
            flush()
            blocks.append(['hr'])
        elif m := re.match(r'^(#{1,6})\s+(.*)$', s):
            flush()
            blocks.append(['h', len(m.group(1)), ann.inline(m.group(2).strip('# '))])
        elif s.startswith('|'):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c):
                    rows.append([ann.inline(c) for c in cells])
                i += 1
            blocks.append(['table', rows])
            continue
        elif LIST_ITEM.match(line):
            flush()
            kind = 'ol' if ORDERED.match(line) else 'ul'
            items = []
            while i < len(lines) and (LIST_ITEM.match(lines[i]) or
                                      (lines[i].startswith('  ') and lines[i].strip() and items)):
                if LIST_ITEM.match(lines[i]):
                    items.append(LIST_ITEM.sub('', lines[i], count=1).strip())
                else:
                    items[-1] += ' ' + lines[i].strip()
                i += 1
            blocks.append([kind, [ann.inline(t) for t in items]])
            continue
        elif s.startswith('>'):
            flush()
            blocks.append(['bq', ann.inline(s.lstrip('> '))])
        else:
            para.append(line)
        i += 1
    flush()
    return blocks, finalize(ann.cites)


def tally(cites):
    out = {}
    for c in cites:
        if c['kind'] == 'ref':
            continue
        out[c['status']] = out.get(c['status'], 0) + 1
    return out
