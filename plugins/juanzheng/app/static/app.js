// 卷證 — 前端。資料全部來自本機伺服器的 /api；回答的解析與引文比對在伺服器端（render.py）完成。
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const api = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  };

  const STATUS = {
    exact: { label: '逐字吻合', cls: 'exact', why: '引文與本頁文字逐字相同。' },
    variant: { label: '僅異體字不同', cls: 'variant', why: '只差在異體字（標色處），視為同一段文字。' },
    near: { label: '相近', cls: 'near', why: '和轉寫有幾個字不同（標色處）。可能是模型改寫，也可能是轉寫讀錯，請看原圖。' },
    miss: { label: '找不到', cls: 'miss', why: '本頁文字裡沒有這段。回答可能改寫或記錯了原文，也可能標錯了頁。' },
    cite: { label: '標頁碼', cls: 'cite', why: '這裡只標了出處頁，沒有直接引文可以逐字比對。' },
    unread: { label: '未讀過此頁', cls: 'miss', why: '回答引用了這一頁，但研究過程中沒有讀過它的全文。' },
    nopage: { label: '語料無此頁', cls: 'miss', why: '語料裡沒有這個頁碼，可能是模型寫錯了。' },
    uncited: { label: '未附出處', cls: 'miss', why: '這段「」沒有標出處，在本題讀過的頁裡也找不到這段文字。' },
    ref: { label: '提及頁碼', cls: 'cite', why: '回答正文提到這一頁，但沒有用【】標成出處，不算引文。' },
  };
  const ICON = {
    search: '<svg class="i" viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>',
    read: '<svg class="i" viewBox="0 0 16 16"><path d="M4 2h6l3 3v9H4z"/><path d="M10 2v3h3M6 8h5M6 10.5h5"/></svg>',
    catalog: '<svg class="i" viewBox="0 0 16 16"><path d="M3 3.5h10M3 8h10M3 12.5h10"/></svg>',
    chev: '<svg class="i chev" viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg>',
    folder: '<svg class="i" viewBox="0 0 16 16"><path d="M2 4.5V12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H8L6.5 3.5H3a1 1 0 0 0-1 1z"/></svg>',
    up: '<svg class="i" viewBox="0 0 16 16"><path d="M8 13V3M4 7l4-4 4 4"/></svg>',
    stop: '<svg class="i" viewBox="0 0 16 16"><rect x="4" y="4" width="8" height="8" rx="1" fill="currentColor"/></svg>',
  };
  const EXAMPLES = [
    '獎勵投資條例施行後，工業用地的編定與取得方式有什麼變化？',
    '監察院對工業用地編定提出了哪些糾正？理由是什麼？',
    '國家安全會議的組織規章前後有哪些修訂？',
  ];

  const pref = (k, d) => { try { return localStorage.getItem('jz.' + k) || d; } catch { return d; } };
  const setPref = (k, v) => { try { localStorage.setItem('jz.' + k, v); } catch { /* 無痕模式 */ } };

  const state = {
    status: null, topics: [], topic: null, sel: null, view: null, tab: 'scan', zoom: 1,
    model: pref('model', 'claude-opus-5-5'), effort: pref('effort', 'medium'), scope: pref('scope', 'all'),
    job: null, live: null, pages: {},
  };

  // ── 資料 ──
  async function refreshStatus() {
    state.status = await api('/api/status');
    renderFoot(); renderPicker(); renderScope(); renderWs(); updateSend();
  }
  async function loadTopics() { state.topics = await api('/api/topics'); renderTopics(); }
  const ws = () => state.status?.workspace;
  // 頁碼可能含中文與底線（文件代號/p3）；圖片網址帶專案路徑，瀏覽器快取才不會跨專案錯用同名頁碼
  const pageUrl = (pid) => `/api/page/${encodeURIComponent(pid)}`;
  const imgUrl = (pid, w) => `/api/img/${encodeURIComponent(pid)}?w=${w}&ws=${encodeURIComponent(ws()?.path || '')}`;
  const shortId = (pid) => (pid.includes('/image') ? `${pid.split('/image')[0]}·${+pid.split('/image')[1]}` : pid.replace('/', '·'));
  async function openTopic(id, { keepSel } = {}) {
    state.topic = await api(`/api/topics/${id}`);
    setPref('topic', id);
    if (!keepSel) {
      const last = [...state.topic.turns].reverse().find((t) => t.status === 'done' && t.cites?.length);
      state.sel = last ? { turn: last.id, n: 0 } : null;
      state.view = null;
    }
    renderTopics(); renderThread(); renderEvidence();
  }
  function turnById(id) { return state.topic?.turns.find((t) => t.id === id); }
  function selCite() { const t = state.sel && turnById(state.sel.turn); return t ? t.cites[state.sel.n] : null; }

  // ── 左欄 ──
  function renderTopics() {
    $('#topics').innerHTML = state.topics.length ? state.topics.map((t) =>
      `<button class="topic ${t.id === state.topic?.id ? 'on' : ''}" data-topic="${t.id}">
        <span class="t">${esc(t.title)}</span><span class="m">${t.updated.slice(5, 10).replace('-', '/')}・${t.turns} 輪</span></button>`).join('')
      : `<div class="side-empty">${ws() ? '還沒有研究題目' : '先開啟一個研究專案'}</div>`;
    $('#newTopic').disabled = !ws();
    const t = state.topic;
    $('#topicTitle').textContent = t ? t.title : (ws()?.name || '卷證');
    const done = t ? t.turns.filter((x) => x.status === 'done').length : 0;
    $('#topicSub').textContent = t ? `${done} 輪問答・最後更新 ${t.updated.slice(5, 16).replace('T', ' ')}` : '';
    $('#exportBtn').disabled = !done;
  }
  function versionHtml(v) {
    if (!v?.version) return '';
    const text = v.dev ? `卷證 ${v.version} 開發版${v.commit ? `・${v.commit}` : ''}` : `卷證 ${v.version}`;
    const tip = v.dev ? '從 NAA_TR repo 直接執行的開發版；版號是 plugin.json 目前的值，未必已發版' : 'Claude Code 外掛版';
    return `<div class="ver" title="${tip}">${esc(text)}</div>`;
  }
  function renderFoot() {
    const s = state.status, a = s.auth, c = s.corpus;
    const limited = s.rate && s.rate.status && s.rate.status !== 'allowed'
      ? `<div class="row" style="color:var(--miss)">${s.rate.status === 'rejected' ? '訂閱額度已用完' : '訂閱額度快用完了'}</div>` : '';
    $('#sideFoot').innerHTML = `<div class="usage" id="usage">${usageHtml(state.usage)}</div>${limited}
      <div class="row"><span class="dot ${a.ok ? '' : 'bad'}"></span>${esc(a.message)}</div>
      <div>${c ? `source ${c.vols} 份・${c.pages.toLocaleString()} 頁` : '尚未開啟研究專案'}</div>
      ${c?.naa ? '<div>轉寫頁是模型轉寫，非人工校訂</div>' : ''}
      ${versionHtml(state.status?.version)}`;
  }

  // ── 研究專案（工作資料夾）──
  const fmtN = (n) => (n || 0).toLocaleString();
  function sourceNotes(src, c) {
    const out = [];
    if (!c || !c.pages) out.push(['warn', 'source 裡還沒有讀得到字的文件。放 PDF、txt、md、epub 或文件資料夾進去，或用下面的「匯入素材包…」，再按「重新讀取」。']);
    if (src?.pack_layout) out.push(['warn', `source 裡有素材包原樣（${src.pack_layout.map((x) => (x === '.' ? 'source 根目錄' : x)).join('、')}）：讀得到但沒有原件與注意事項，原始 PDF 會變成讀不到字的文件。建議改用「匯入素材包…」轉成文件資料夾，再把原樣搬出 source。`]);
    (src?.warnings || []).forEach((w) => out.push(['warn', w]));
    if (src?.no_text_pages) out.push(['warn', `${fmtN(src.no_text_pages)} 頁沒有文字層（掃描檔），讀不到字、搜不到`, src.no_text.join('\n')]);
    if (src?.skipped_total) out.push(['', `略過 ${fmtN(src.skipped_total)} 個檔（格式不支援或讀不了）`, src.skipped.map((x) => `${x.rel}：${x.why}`).join('\n')]);
    if (src?.duplicates) out.push(['', `${fmtN(src.duplicates)} 頁重複出現，以先讀到的為準`]);
    return out;
  }
  function contextLine(ctx) {
    const used = ctx.files.filter((f) => f.used);
    return used.length ? `${used.length} 個檔、${fmtN(ctx.used)} 字，每題自動帶入` : '空的（可放 .md／.txt 研究指示）';
  }
  function renderWs() {
    const w = ws(), c = state.status?.corpus;
    $('#wsBtn').innerHTML = `${ICON.folder}<span class="ws-name">${w
      ? `<b>${esc(w.name)}</b><small>${c ? `${fmtN(c.vols)} 份・${fmtN(c.pages)} 頁` : ''}${w.context.used ? '・有 context' : ''}</small>`
      : '<b>選擇研究專案</b><small>還沒開啟工作資料夾</small>'}</span>${ICON.chev}`;
    $('#wsBtn').title = w ? `${w.path}\n點一下切換研究專案` : '開啟或新建研究專案';
    if (!$('#wsMenu').hidden) $('#wsMenu').innerHTML = wsMenuHtml();
  }
  function wsMenuHtml() {
    const s = state.status, w = s.workspace, c = s.corpus;
    let h = '';
    if (w) {
      const notes = [...sourceNotes(w.source, c)];
      if (w.context.files.some((f) => f.truncated)) notes.push(['warn', `context 超過 ${fmtN(w.context.max)} 字，後面的沒帶入`]);
      if (w.context.ignored.length) notes.push(['', `context 略過 ${w.context.ignored.length} 項（只讀根目錄的 .md／.txt）`, w.context.ignored.join('\n')]);
      if (w.warning) notes.unshift(['bad', w.warning]);
      const row = (k, v, sub) => `<div class="wm-row"><span class="k">${k}</span><span class="v">${v}</span>
        <button class="wm-mini" data-ws="reveal" data-sub="${sub}" title="在檔案總管開啟 ${k} 資料夾">開啟</button></div>`;
      h += `<div class="wm-cur"><div class="wm-path" title="${esc(w.path)}">${esc(w.path)}</div>
        ${row('source', c ? `${fmtN(c.vols)} 份・${fmtN(c.pages)} 頁` : '—', 'source')}
        ${row('context', contextLine(w.context), 'context')}
        ${row('session', `${fmtN(w.topics)} 個研究題目`, 'session')}
        ${notes.map(([cls, t, tip]) => `<div class="wm-note ${cls}" ${tip ? `title="${esc(tip)}"` : ''}>${esc(t)}</div>`).join('')}
        <div class="wm-actions"><button class="wm-mini" data-ws="reload" title="source 或 context 有新增、修改檔案後按這裡">重新讀取 source</button>
        <button class="wm-mini" data-ws="reveal" data-sub="" title="在檔案總管開啟專案資料夾">開啟資料夾</button></div></div>`;
    }
    const others = s.recent.filter((r) => !w || r.path !== w.path);
    if (others.length) {
      h += `<div class="pm-label">最近的專案</div>${others.map((r) => `<div class="wm-recent ${r.ok ? '' : 'gone'}" data-ws-open="${esc(r.path)}" title="${esc(r.path)}${r.ok ? '' : '\n（找不到這個資料夾，或它不是卷證專案）'}">
        ${ICON.folder}<span class="ws-name"><b>${esc(r.name)}</b><small>${esc(r.path)}</small></span>
        <span class="x" data-ws-forget="${esc(r.path)}" title="從清單移除（不會刪資料夾）">×</span></div>`).join('')}`;
    }
    h += `<div class="wm-sep"></div>
      <button class="wm-item" data-ws="open">${ICON.folder}開啟專案資料夾…</button>
      <button class="wm-item" data-ws="new"><svg class="i" viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>新建專案…</button>
      ${w ? `<button class="wm-item" data-ws="import" title="把素材包（原始PDF、逐頁JPG、轉寫文字、pages.jsonl、manifest.csv）轉成文件資料夾，放進這個專案的 source">${ICON.up}匯入素材包到 source…</button>` : ''}
      ${s.legacy ? `<button class="wm-item" data-ws="migrate">${ICON.up}把 ${s.legacy} 個舊題目搬進新專案…</button>` : ''}`;
    return h;
  }
  function toggleWs(open) {
    const menu = $('#wsMenu');
    open = open ?? menu.hidden;
    if (open) menu.innerHTML = wsMenuHtml();
    menu.hidden = !open;
    $('#wsBtn').setAttribute('aria-expanded', String(open));
  }
  async function pickFolder(why) {
    if (window.pywebview?.api?.pick_folder) return window.pywebview.api.pick_folder(ws()?.path ? ws().path.replace(/[\\/][^\\/]+$/, '') : '');
    // 瀏覽器（開發用）沒有選資料夾的對話框，只能貼路徑
    return (prompt(`${why}\n（請貼上資料夾的完整路徑；桌面 app 會跳出選資料夾的對話框）`, '') || '').trim() || null;
  }
  async function switched() {
    state.topic = null; state.sel = null; state.view = null; state.pages = {};
    toggleWs(false);
    await refreshStatus();
    await loadTopics();
    const first = state.topics[0];
    if (first) await openTopic(first.id); else { renderTopics(); renderThread(); renderEvidence(); }
  }
  async function wsAction(el) {
    const act = el.dataset.ws;
    try {
      if (el.dataset.wsForget) { await api('/api/workspace/forget', { path: el.dataset.wsForget }); return refreshStatus().then(() => toggleWs(true)); }
      if (el.dataset.wsOpen) { await api('/api/workspace/open', { path: el.dataset.wsOpen }); return switched(); }
      if (act === 'reveal') return api('/api/workspace/reveal', { sub: el.dataset.sub || '' });
      if (act === 'reload') { el.textContent = '讀取中…'; await api('/api/workspace/reload', {}); return switched(); }
      if (act === 'open') {
        const path = await pickFolder('選擇研究專案的資料夾');
        if (!path) return;
        let r = await api('/api/workspace/open', { path });
        if (r.need_init) {
          const more = r.entries ? `\n\n這個資料夾裡已經有 ${r.entries} 個項目，不會動到它們，只會補上缺的子資料夾。` : '';
          if (!confirm(`「${r.path}」還不是卷證專案。\n要在裡面建立 ${r.missing.join('、')} 子資料夾，把它當成研究專案嗎？${more}`)) return;
          r = await api('/api/workspace/open', { path, init: true });
        }
        return switched();
      }
      if (act === 'import') {
        const path = await pickFolder('選擇素材包資料夾（裡面有 pages.jsonl、manifest.csv、原始PDF）');
        if (!path) return;
        const images = confirm('要一起複製「逐頁JPG」嗎？\n\n平常不需要：原圖會直接從原始 PDF 畫出來，可以省下好幾 GB。\n按「確定」複製，按「取消」不複製（建議）。');
        el.textContent = '匯入中…（複製原始 PDF，可能要一兩分鐘）';
        const r = await api('/api/workspace/import', { path, images });
        await switched();
        alert(`匯入完成：新建 ${r.made} 卷${r.images ? `（含 ${r.images} 張逐頁 JPG）` : ''}。`
          + (r.kept.length ? `\n已經有、沒動：${r.kept.length} 卷。` : '')
          + (r.catalog ? `\n文件總表 _catalog.csv 新增 ${r.catalog} 列（重複掃描、合成卷號等注意事項）。` : '')
          + (r.problems.length ? `\n\n沒匯入（請檢查）：\n${r.problems.join('\n')}` : ''));
        return;
      }
      if (act === 'new' || act === 'migrate') {
        const name = (prompt(act === 'new'
          ? '新研究專案的名稱（會建立同名資料夾，底下有 source、context、session）\n按確定後，選這個專案要放在哪個資料夾。'
          : `把 ${state.status.legacy} 個舊研究題目搬進新專案。\n新專案的名稱：（按確定後選它要放在哪個資料夾；轉寫總表會依卷拆成文件資料夾放進 source）`,
        act === 'new' ? '' : '檔案管理局轉寫語料') || '').trim();
        if (!name) return;
        const parent = await pickFolder(`選擇「${name}」要放在哪個資料夾`);
        if (!parent) return;
        el.textContent = act === 'new' ? '建立中…' : '搬移中…';
        const r = await api(`/api/workspace/${act === 'new' ? 'create' : 'migrate'}`, { parent, name });
        await switched();
        if (act === 'migrate') alert(`已搬進「${name}」：${r.moved} 個題目。${r.volumes ? `\n轉寫總表已依卷拆成 ${r.volumes} 個文件資料夾放進 source。` : ''}\n原本的資料夾改名留著當備份：\n${r.backup}`);
      }
    } catch (ex) { alert(ex.message); renderWs(); if (!$('#wsMenu').hidden) toggleWs(true); }
  }
  // 訂閱用量：伺服器跑 Claude Code 的 /usage（不呼叫模型、不耗額度）
  function usageHtml(u) {
    const head = `<div class="u-head"><span>訂閱用量</span><button data-act="usage" title="重新查詢（不耗額度）">更新</button></div>`;
    if (!u) return head + '<div class="u-reset">查詢中…</div>';
    if (!u.ok) return head + `<div class="u-reset">${esc(u.error || (u.raw || '').slice(0, 160) || '查不到用量')}</div>`;
    return head + u.bars.map((b) => {
      const cls = b.pct >= 90 ? 'full' : b.pct >= 70 ? 'warn' : '';
      return `<div class="u-row"><span class="u-label">${esc(b.label)}</span><span class="u-pct">${Math.round(b.pct)}%</span>
        <div class="u-bar"><i class="${cls}" style="width:${Math.min(100, b.pct)}%"></i></div>
        ${b.resets ? `<span class="u-reset">${esc(b.resets)} 重置</span>` : ''}</div>`;
    }).join('');
  }
  async function refreshUsage(force) {
    try { state.usage = await api('/api/usage' + (force ? '?refresh=1' : '')); } catch (ex) { state.usage = { ok: false, error: ex.message }; }
    const el = $('#usage'); if (el) el.innerHTML = usageHtml(state.usage);
  }

  // ── 中欄 ──
  const fmtTime = (s) => (s >= 60 ? `${Math.floor(s / 60)} 分 ${String(Math.round(s % 60)).padStart(2, '0')} 秒` : `${Math.round(s)} 秒`);
  const modelLabel = (id) => state.status?.models.find((m) => m.id === id)?.label || id;
  function chip(turn, n) {
    const c = turn.cites[n];
    if (!c.page) return '';
    const st = STATUS[c.status] || STATUS.cite;
    const on = state.sel && state.sel.turn === turn.id && state.sel.n === n;
    return `<button class="cite ${st.cls} ${c.read ? '' : 'unread'} ${on ? 'sel' : ''}" data-turn="${turn.id}" data-n="${n}"
      title="${esc(c.page)}　${esc(c.title || '')}｜${st.label}${c.read ? '' : '｜研究中沒讀過此頁'}${c.auto ? '｜出處由程式自動找到' : ''}${c.stale ? '｜原文在核對之後變動過' : ''}${c.gone ? '｜這一頁已不在 source' : ''}"><i></i>${esc(c.short || shortId(c.page))}${c.auto ? '<em>自動</em>' : ''}${c.stale || c.gone ? '<em>⚠</em>' : ''}</button>`;
  }
  function inline(turn, parts) {
    return parts.map((p) => {
      if (typeof p === 'string') return esc(p).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
      if (p.q !== undefined) {
        const c = turn.cites[p.q], st = STATUS[c.status] || STATUS.cite;
        const on = state.sel && state.sel.turn === turn.id && state.sel.n === p.q;
        return `<span class="quote ${st.cls} ${on ? 'sel' : ''}" data-turn="${turn.id}" data-n="${p.q}">「${esc(c.quote)}」</span>${chip(turn, p.q)}`;
      }
      return chip(turn, p.c);
    }).join('');
  }
  function blocksHtml(turn) {
    return turn.blocks.map((b) => {
      switch (b[0]) {
        case 'h': return `<h3>${inline(turn, b[2])}</h3>`;
        case 'p': return `<p>${inline(turn, b[1])}</p>`;
        case 'ul': case 'ol': return `<${b[0]}>${b[1].map((li) => `<li>${inline(turn, li)}</li>`).join('')}</${b[0]}>`;
        case 'bq': return `<blockquote>${inline(turn, b[1])}</blockquote>`;
        case 'hr': return '<hr>';
        case 'table': return `<div class="tbl"><table>${b[1].map((row, i) => `<tr>${row.map((cell) =>
          `<${i ? 'td' : 'th'}>${inline(turn, cell)}</${i ? 'td' : 'th'}>`).join('')}</tr>`).join('')}</table></div>`;
        default: return '';
      }
    }).join('');
  }
  function tallyHtml(turn) {
    const n = {};
    turn.cites.forEach((c) => { if (c.kind !== 'ref') n[c.status] = (n[c.status] || 0) + 1; });
    return ['exact', 'variant', 'near', 'miss', 'uncited', 'unread', 'nopage', 'cite'].filter((s) => n[s])
      .map((s) => `<span class="pill ${STATUS[s].cls}">${STATUS[s].label} ${n[s]}</span>`).join('') || '<span class="hint">沒有引文</span>';
  }
  function trailHtml(trail, live) {
    return `<ol>${trail.map(([k, what, note], i) =>
      `<li class="${live && i === trail.length - 1 ? 'live' : ''}">${ICON[k] || ICON.read}<span>${
        k === 'search' ? `搜尋「${esc(what)}」` : k === 'read' ? `讀 <code>${esc(what)}</code>` : esc(what)}</span><span class="n">${esc(note)}</span></li>`).join('')}</ol>`;
  }
  function turnHtml(t) {
    let h = `<div class="q">${esc(t.q)}</div>`;
    const meta = `<span class="model">${esc(modelLabel(t.model))}</span><span class="sep"></span><span class="mono">${t.effort}</span>`;
    if (t.status === 'done') {
      h += `<div class="a" id="turn-${t.id}">
        <div class="a-meta">${meta}<span class="sep"></span><span>${fmtTime(t.secs || 0)}</span>${t.scope !== 'all' ? `<span class="sep"></span><span>${esc(state.status.scopes[t.scope] || t.scope.replace(/^g:/, ''))}</span>` : ''}${planHtml(t)}</div>
        <details class="trail"><summary>${ICON.search}研究足跡：搜尋 ${t.searches} 次、讀 ${t.reads} 頁 ${ICON.chev}</summary>${trailHtml(t.trail)}</details>
        <div class="body">${blocksHtml(t)}</div>
        <div class="tally">引文核對 ${tallyHtml(t)}
          <span class="actions"><button class="ghost" data-copy="${t.id}" title="複製回答原文">複製</button></span></div></div>`;
    } else if (t.status === 'running' && state.live && state.live.turn === t.id) {
      const L = state.live, lim = state.status.limits[t.effort];
      h += `<div class="a"><div class="running"><span class="spinner"></span>研究中・${esc(modelLabel(t.model))}・<span class="mono">${t.effort}</span>・${fmtTime(L.secs)}／上限 ${lim} 分鐘</div>
        ${L.trail.length ? `<details class="trail" open><summary>${ICON.search}研究足跡 ${ICON.chev}</summary>${trailHtml(L.trail, true)}</details>` : ''}</div>`;
    } else if (t.status === 'running') {
      h += `<div class="a"><div class="a-meta">${meta}<span class="sep"></span><span>這一題沒有完成（伺服器可能重啟過）。</span></div></div>`;
    } else if (t.status === 'stopped') {
      h += `<div class="a"><div class="a-meta">${meta}<span class="sep"></span><span>已停止，未完成的回答不會保存。</span></div></div>`;
    } else {
      h += `<div class="a"><div class="a-meta">${meta}</div><div class="err">${esc(t.error || '發生錯誤')}</div></div>`;
    }
    return h;
  }
  function planHtml(t) {
    const d = (t.plan || []).find((x) => x.key === 'Current session');
    if (!d) return '';
    const v = d.delta < 1 ? '不到 1%' : `約 ${Math.round(d.delta)}%`;
    return `<span class="sep"></span><span class="plan" title="問答前後的訂閱用量差（整數百分比，只是近似）；期間在 Claude Code 或 claude.ai 的用量也會算進來">用掉本時段額度 ${v}</span>`;
  }
  function wsWelcome() {
    const s = state.status;
    const recent = s.recent.filter((r) => r.ok).slice(0, 5);
    return `<div class="welcome">
      <h2>選一個研究專案</h2>
      <p>一個研究專案就是一個資料夾，底下有三個子資料夾：<b>source</b> 放研究材料（PDF、txt、md、epub，或從素材包匯入的文件資料夾），
      <b>context</b> 放研究指示與背景筆記（每題自動帶給 Claude），<b>session</b> 存研究題目與核對紀錄。資料夾放哪裡都可以。</p>
      ${s.ws_error ? `<div class="err" style="margin-bottom:16px">${esc(s.ws_error)}</div>` : ''}
      <div class="ws-cta"><button class="primary" data-ws="new">新建專案…</button><button data-ws="open">開啟專案資料夾…</button></div>
      ${s.legacy ? `<div class="legacy">這台電腦有 ${s.legacy} 個改版前的研究題目，還沒歸入任何專案。
        <button class="link" data-ws="migrate">搬進新專案…</button></div>` : ''}
      ${recent.length ? `<div class="examples" style="margin-top:22px">${recent.map((r) =>
        `<button class="example" data-ws-open="${esc(r.path)}" title="${esc(r.path)}"><b>${esc(r.name)}</b>　<small>${esc(r.path)}</small></button>`).join('')}</div>` : ''}
    </div>`;
  }
  function renderThread() {
    const t = state.topic;
    if (!ws()) { $('#thread').innerHTML = wsWelcome(); return; }
    if (!t || !t.turns.length) {
      const c = state.status?.corpus;
      $('#thread').innerHTML = `<div class="welcome">
        <h2>${t ? '在這個題目下提出第一個問題' : '開一個研究題目'}</h2>
        ${c?.pages ? `<p>Claude 會在「${esc(ws().name)}」source 裡的 ${fmtN(c.vols)} 份、${fmtN(c.pages)} 頁材料裡搜尋、讀頁，回答時每段「」引文都標出處；程式再逐條比對原文，右邊可以看原圖。${ws().context.used ? '<br>context 裡的研究指示會自動帶入。' : ''}</p>`
          : '<p>這個專案的 source 資料夾裡還沒有讀得到字的文件。把 PDF、txt、md 或轉寫總表（pages.jsonl）放進去，再從左上角的專案選單按「重新讀取 source」。</p>'}
        ${c?.naa ? `<div class="examples">${EXAMPLES.map((q) => `<button class="example" data-example="${esc(q)}">${esc(q)}</button>`).join('')}</div>` : ''}</div>`;
      return;
    }
    $('#thread').innerHTML = t.turns.map(turnHtml).join('');
  }

  // ── 右欄 ──
  async function pageData(pid) {
    if (!state.pages[pid]) state.pages[pid] = await api(pageUrl(pid));
    return state.pages[pid];
  }
  // 依文字來源（text_origin，規格 docs/ai-notes/source-format.md 第四節）：同樣「逐字吻合」，意義不同
  const CAVEAT = {
    'llm-transcription': '「逐字吻合」只代表這段字在模型轉寫裡，不代表轉寫讀對了。人名、日期、數字與手寫批示，一律以原圖為準。',
    ocr: '「逐字吻合」只代表這段字在 OCR 結果裡，不代表辨識正確；請對照原頁面。',
    embedded: '「逐字吻合」只代表這段字在 PDF 的文字層裡。掃描檔的文字層若是 OCR 產生的，可能有錯，請對照上面的原頁面。',
    'born-digital': '「逐字吻合」代表這段字確實在原件裡；原件本身可不可靠、是不是一手史料，要另外判斷。',
    human: '「逐字吻合」只代表這段字在人工繕打的文字裡；繕打仍可能有誤，有原頁面時請對照。',
    unknown: '「逐字吻合」只代表這段字在 source 的文字裡；這份文字的來源不明，請以原件為準。',
  };
  function marked(text, s, e, marks) {
    // 在 [s,e) 範圍包 <mark>，marks 內的字另包 .diff；回傳 HTML
    const diff = new Set();
    (marks || []).forEach(([a, b]) => { for (let i = a; i < b; i++) diff.add(i); });
    let out = '';
    for (let i = 0; i < text.length; i++) {
      if (i === s) out += '<mark>';
      const ch = esc(text[i]);
      out += diff.has(i) ? `<b class="diff">${ch}</b>` : ch;
      if (i === e - 1) out += '</mark>';
    }
    return out.replace(/\[\?\]/g, '<span class="unk">[?]</span>').replace(/@@PAGEBREAK/g, '<span class="pb">〔換頁〕</span>');
  }
  function excerpt(text, c) {
    if (c.start == null) return null;
    const a = Math.max(0, c.start - 70), b = Math.min(text.length, c.end + 70);
    const shift = (r) => r.map(([x, y]) => [x - a, y - a]);
    return (a > 0 ? '…' : '') + marked(text.slice(a, b), c.start - a, c.end - a, shift(c.src_marks || [])).replace(/\n/g, '<br>') + (b < text.length ? '…' : '');
  }
  function quoteHtml(c) {
    if (!c.quote_marks) return esc(c.quote);
    return marked(c.quote, -1, -1, c.quote_marks);
  }
  async function renderEvidence() {
    const el = $('#evidence'), c = selCite(), turn = state.sel && turnById(state.sel.turn);
    if (!c) {
      el.innerHTML = '<div class="ev-empty">點回答裡的引文或頁碼標籤，這裡會顯示原圖與比對結果。</div>';
      return;
    }
    const st = STATUS[c.status] || STATUS.cite, pid = state.view || c.page;
    const nav = `<div class="ev-nav"><span class="count">引用 ${state.sel.n + 1} / ${turn.cites.length}</span>
      <div class="ev-dots">${turn.cites.map((x, i) => `<button class="${(STATUS[x.status] || STATUS.cite).cls} ${i === state.sel.n ? 'on' : ''}" data-turn="${turn.id}" data-n="${i}" title="${esc(x.page || '無出處')}">${i + 1}</button>`).join('')}</div>
      <button class="icon-btn" data-act="prev" title="上一條">‹</button><button class="icon-btn" data-act="next" title="下一條">›</button></div>`;
    const vol = !c.page ? '' : (c.doc_kind || 'naa') === 'naa' ? `${c.page.split('/')[0]} ${c.title || ''}` : (c.title || '');
    const head = `<div class="ev-head"><div class="ev-id"><code>${esc(c.page || '（無出處）')}</code><span class="badge pill ${st.cls}">${st.label}</span></div>
      <div class="ev-vol">${esc(vol)}${c.duplicate_of ? `　⚠ 與 ${c.duplicate_of} 卷為同一份掃描` : ''}</div>
      ${c.stale ? '<div class="why stale">⚠ 這一頁的文字在本題核對之後變動過（source 改過或換了新版轉寫）。上面的核對結果是當時的，請重看。</div>' : ''}
      ${c.gone ? '<div class="why stale">⚠ 這一頁在目前的 source 裡已經不存在（檔案刪了或改名），只剩當時的核對結果。</div>' : ''}</div>`;
    if (!pid || c.status === 'nopage') {
      el.innerHTML = nav + `<div class="ev-body">${head}<div class="check">${c.quote ? `<h4>回答寫的</h4><div class="said">「${esc(c.quote)}」</div>` : ''}<div class="why">${st.why}</div></div></div>`;
      return;
    }
    el.innerHTML = nav + `<div class="ev-body">${head}<div class="ev-loading">載入中…</div></div>`;
    let p;
    try { p = await pageData(pid); } catch (ex) {
      $('.ev-loading', el).textContent = `讀不到這一頁：${ex.message}`; return;
    }
    if (selCite() !== c || (state.view || c.page) !== pid) return;   // 載入期間已切到別條
    const browsing = pid !== c.page, kind = p.doc_kind || 'naa';
    const word = kind === 'naa' ? '轉寫' : '文字';
    const tabs = `<div class="tabs">${[['scan', p.has_image ? '原圖與核對' : '核對'], ['text', `${word}全文`], ['meta', '來源']].map(([k, l]) =>
      `<button class="${state.tab === k ? 'on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>`;
    let body;
    if (state.tab === 'scan') {
      const img = p.has_image
        ? `<img class="sheet" src="${imgUrl(pid, 1400)}" alt="${esc(pid)} 原件" style="width:${Math.round(state.zoom * 100)}%">`
        : `<div class="noimg">${kind === 'text' ? '文字檔沒有原圖；全文在「文字全文」分頁。' : '這一頁的原圖不在本機（專案 source 或 repo 的 sample/ 資料夾）。'}</div>`;
      body = `<div class="viewer ${p.has_image ? '' : 'short'}">${img}</div>
        <div class="viewer-bar">
          <button data-page="${esc(p.prev || '')}" ${p.prev ? '' : 'disabled'} title="前一頁">‹ 前頁</button>
          <button data-page="${esc(p.next || '')}" ${p.next ? '' : 'disabled'} title="後一頁">後頁 ›</button>
          <span class="vb-note">${browsing ? `正在看鄰頁 <code>${esc(pid)}</code>・<button class="link" data-act="back">回到引用頁</button>` : kind === 'text' ? '文字檔依段落切段，p 是第幾段' : `${word}沒有座標，只能定位到整頁`}</span>
          ${p.has_image ? '<button data-act="zout" title="縮小">−</button><button data-act="zin" title="放大">＋</button>' : ''}
          <button data-act="open" title="${kind === 'naa' ? '用系統看圖程式開原檔' : '用系統預設程式開這份文件'}">原檔</button></div>`;
      if (!browsing) {
        const ex = c.status === 'uncited' ? null : excerpt(p.text, c);
        body += `<div class="check">
          ${c.quote ? `<h4>回答寫的</h4><div class="said">「${quoteHtml(c)}」</div>` : ''}
          ${p.note && kind !== 'naa' ? `<div class="why">${esc(p.note)}</div>` : ''}
          ${ex ? `<h4>本頁${word}（命中處前後）</h4><div class="src">${ex}</div>` : c.quote || !p.text ? '' : `<h4>本頁${word}（開頭）</h4><div class="src">${esc(p.text.slice(0, 160))}…</div>`}
          <div class="why">${st.why}${c.score && c.status !== 'exact' ? `相似度 ${Math.round(c.score)}%。` : ''}${c.auto ? '出處是程式在本題讀過的頁裡自動找到的。' : ''}${!c.read && c.kind === 'quote' ? '注意：研究過程中沒有讀過這一頁的全文。' : ''}</div>
          <h4>${p.has_image ? '你看原圖的判斷' : '你核對後的判斷'}</h4>
          <div class="verdict">${[['ok', '✓ 相符'], ['bad', '✗ 有誤'], ['unsure', '? 看不清']].map(([v, l]) =>
            `<button class="${v === 'ok' ? 'ok' : v === 'bad' ? 'bad' : 'unsure'} ${c.verdict === v ? 'on' : ''}" data-verdict="${v}">${l}</button>`).join('')}</div>
          <textarea class="note" id="note" placeholder="註記（選填，離開欄位時儲存）：例如原圖其實作「逾」">${esc(c.note || '')}</textarea></div>
          <div class="caveat">${CAVEAT[p.text_origin] || CAVEAT.unknown}</div>`;
      }
    } else if (state.tab === 'text') {
      const hl = !browsing && c.start != null ? marked(p.text, c.start, c.end, c.src_marks) : marked(p.text, -1, -1);
      body = p.text ? `<div class="text-tab">${hl.split('\n').map((l, i) => `<div class="ln"><span>${i + 1}</span><div>${l || '&nbsp;'}</div></div>`).join('')}</div>`
        : `<div class="ev-empty">${esc(p.note || '這一頁沒有文字。')}</div>`;
    } else {
      const naa = kind === 'naa';
      body = `<div class="meta-tab"><dl>
        <dt>引用編號</dt><dd><code>${esc(pid)}</code></dd><dt>${naa ? '案卷' : '文件'}</dt><dd>${esc(p.title)}（${esc(p.batch)}）</dd>
        <dt>文字來源</dt><dd>${esc(p.origin || '')}</dd>
        ${p.label ? `<dt>${kind === 'pdf' ? '紙上頁碼' : '章節'}</dt><dd>${esc(p.label)}</dd>` : ''}
        ${naa ? `<dt>可讀度</dt><dd>${esc(p.legibility ?? '—')}（模型自報）</dd><dt>[?] 區塊</dt><dd>${p.unread_marks ?? '—'}</dd>
        <dt>page_id</dt><dd><code>${esc(p.cid || '—')}</code></dd>` : ''}
        <dt>原檔</dt><dd><code>${esc(p.rel || '')}</code></dd>${p.note ? `<dt>注意</dt><dd>${esc(p.note)}</dd>` : ''}</dl></div>`;
    }
    el.innerHTML = nav + `<div class="ev-body">${head}${tabs}${body}</div>`;
  }
  function select(turnId, n) {
    state.sel = { turn: turnId, n: +n }; state.view = null; state.zoom = 1;
    document.getElementById('app').classList.remove('no-evidence');
    $('#evToggle').classList.add('on');
    renderThread(); renderEvidence();
  }

  // ── 輸入區 ──
  function renderPicker() {
    const s = state.status;
    if (!s.models.some((m) => m.id === state.model)) state.model = s.models[0].id;
    $('#pickerBtn').innerHTML = `${esc(modelLabel(state.model))} <span class="eff">${state.effort}</span>
      <svg class="i" viewBox="0 0 16 16"><path d="M4 10l4-4 4 4"/></svg>`;
    const desc = { 'claude-opus-5-5': '推理最強，較耗額度', 'claude-sonnet-5-5': '速度與額度較平衡' };
    $('#models').innerHTML = s.models.map((m) => `<button type="button" class="pm-model ${m.id === state.model ? 'on' : ''}" data-model="${m.id}"><b>${esc(m.label)}</b><small>${desc[m.id] || ''}</small></button>`).join('');
    [...$('#effort').children].forEach((b) => b.classList.toggle('on', b.dataset.v === state.effort));
    $('#limitHint').textContent = `單題上限 ${s.limits[state.effort]} 分鐘；愈高愈慢、愈耗額度`;
  }
  function renderScope() {
    // 範圍＝source 的分組（轉寫總表的「第一批／第二批」，或 source 底下第一層子資料夾）
    const s = state.status, c = s.corpus, b = c?.batches || {};
    if (!s.scopes[state.scope]) state.scope = 'all';
    const label = (k) => (k === 'all' ? `全部（${fmtN(c?.vols)} 份）` : `${s.scopes[k]}（${fmtN(b[s.scopes[k]]?.vols)} 份）`);
    $('#scope').innerHTML = Object.keys(s.scopes).map((k) => `<option value="${esc(k)}" ${k === state.scope ? 'selected' : ''}>${esc(label(k))}</option>`).join('');
    $('#scope').closest('.mini-select').hidden = Object.keys(s.scopes).length < 3;      // 只有一組時不用選
  }
  function updateSend() {
    const s = $('#send'), running = !!state.job, ok = state.status?.auth.ok && !!ws();
    s.classList.toggle('stop', running);
    s.title = running ? '停止' : !ws() ? '先開啟一個研究專案' : ok ? '送出' : state.status?.auth.message || '';
    s.innerHTML = running ? ICON.stop : ICON.up;
    s.disabled = !running && !ok;
  }
  function togglePicker(open) {
    const menu = $('#pickerMenu');
    open = open ?? menu.hidden;
    menu.hidden = !open;
    $('#pickerBtn').setAttribute('aria-expanded', String(open));
  }

  // ── 問答 ──
  async function ask() {
    const q = $('#input').value.trim();
    if (!q || state.job) return;
    if (!ws()) return alert('先開啟或新建一個研究專案（左上角）。');
    if (!state.topic) await newTopic();
    togglePicker(false);
    let r;
    try {
      r = await api(`/api/topics/${state.topic.id}/ask`, { question: q, model: state.model, effort: state.effort, scope: state.scope });
    } catch (ex) { return alert(ex.message); }
    $('#input').value = '';
    await openTopic(state.topic.id, { keepSel: true });
    follow(r.job, r.turn);
  }
  function follow(jobId, turnId) {
    state.job = jobId;
    state.live = { turn: turnId, trail: [], secs: 0 };
    updateSend(); renderThread(); scrollEnd();
    const started = Date.now();
    const tick = setInterval(() => { if (state.live) { state.live.secs = (Date.now() - started) / 1000; patchRunning(); } }, 1000);
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.onmessage = async (m) => {
      const e = JSON.parse(m.data);
      if (e.type === 'search' || e.type === 'read' || e.type === 'catalog') {
        state.live.trail.push([e.type, e.what, e.note]); renderThread(); scrollEnd();
      } else if (e.type === 'rate') {
        state.status.rate = e; renderFoot();
      } else if (e.type === 'usage') {
        state.usage = e; const el = $('#usage'); if (el) el.innerHTML = usageHtml(state.usage);
      } else if (['done', 'error', 'stopped'].includes(e.type)) {
        es.close(); clearInterval(tick);
        const topicId = state.topic?.id;
        state.job = null; state.live = null; updateSend();
        await loadTopics();
        const job = e.turn;
        if (topicId) await openTopic(topicId, { keepSel: true });
        if (e.type === 'done' && job.cites?.length && state.topic?.turns.some((t) => t.id === job.id)) select(job.id, 0);
        $(`#turn-${job.id}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    };
    es.onerror = () => { /* 瀏覽器會自動重連；伺服器會補送漏掉的事件 */ };
  }
  function patchRunning() {
    const el = $('.running');
    if (el && state.live) el.innerHTML = el.innerHTML.replace(/・[^・]*／上限/, `・${fmtTime(state.live.secs)}／上限`);
  }
  const scrollEnd = () => { const m = $('#messages'); m.scrollTop = m.scrollHeight; };
  async function newTopic() {
    if (!ws()) return toggleWs(true);
    state.topic = await api('/api/topics', { title: '' });
    state.sel = null;
    await loadTopics(); renderTopics(); renderThread(); renderEvidence();
    $('#input').focus();
  }

  // ── 事件 ──
  document.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-ws-forget],[data-ws-open],[data-ws],[data-topic],[data-n],[data-tab],[data-act],[data-verdict],[data-page],[data-copy],[data-example],[data-model]');
    if (!t) return;
    if (t.dataset.ws !== undefined || t.dataset.wsOpen || t.dataset.wsForget) {
      if (state.job && t.dataset.ws !== 'reveal') return alert('有題目正在研究，等它完成或停止後再切換專案。');
      return wsAction(t);
    }
    if (t.dataset.topic) return openTopic(t.dataset.topic);
    if (t.dataset.model) { state.model = t.dataset.model; setPref('model', state.model); return renderPicker(); }
    if (t.dataset.n !== undefined) return select(t.dataset.turn, t.dataset.n);
    if (t.dataset.tab) { state.tab = t.dataset.tab; return renderEvidence(); }
    if (t.dataset.example) { $('#input').value = t.dataset.example; return $('#input').focus(); }
    if (t.dataset.copy) { await navigator.clipboard.writeText(turnById(t.dataset.copy).answer); t.textContent = '已複製'; return; }
    if (t.dataset.page !== undefined) { if (t.dataset.page) { state.view = t.dataset.page; state.zoom = 1; renderEvidence(); } return; }
    if (t.dataset.verdict) {
      const c = selCite(), v = c.verdict === t.dataset.verdict ? null : t.dataset.verdict;
      await api('/api/verdict', { topic: state.topic.id, turn: state.sel.turn, n: state.sel.n, verdict: v, note: $('#note')?.value || '' });
      c.verdict = v; c.note = $('#note')?.value || ''; return renderEvidence();
    }
    const turn = state.sel && turnById(state.sel.turn), n = state.sel?.n ?? 0;
    ({
      prev: () => select(turn.id, (n - 1 + turn.cites.length) % turn.cites.length),
      next: () => select(turn.id, (n + 1) % turn.cites.length),
      back: () => { state.view = null; renderEvidence(); },
      usage: () => { $('#usage .u-head button').textContent = '查詢中…'; refreshUsage(true); },
      zin: () => { state.zoom = Math.min(state.zoom + 0.25, 3); renderEvidence(); },
      zout: () => { state.zoom = Math.max(state.zoom - 0.25, 0.5); renderEvidence(); },
      open: () => api('/api/open', { page: state.view || selCite().page }).catch((ex) => alert(`開不了原檔：${ex.message}`)),
    }[t.dataset.act] || (() => {}))();
  });
  document.addEventListener('focusout', async (e) => {
    if (e.target.id !== 'note') return;
    const c = selCite();
    if (!c || (c.note || '') === e.target.value) return;
    await api('/api/verdict', { topic: state.topic.id, turn: state.sel.turn, n: state.sel.n, verdict: c.verdict || null, note: e.target.value });
    c.note = e.target.value;
  });

  // ── 左欄收合（Ctrl+B）與右欄拖拉調寬，都記在這台電腦 ──
  const appEl = document.getElementById('app');
  function setSide(collapsed) {
    appEl.classList.toggle('side-collapsed', collapsed);
    setPref('side', collapsed ? 'closed' : 'open');
    const w = +pref('evw', 0);
    if (w) setEvWidth(w);           // 收合時拉寬的右欄，展開左欄後要重新限制，中欄才不會被擠扁
  }
  $('#sideClose').addEventListener('click', () => setSide(true));
  $('#sideOpen').addEventListener('click', () => setSide(false));
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b') {
      e.preventDefault(); setSide(!appEl.classList.contains('side-collapsed'));
    }
  });
  const EV_MIN = 340;
  function setEvWidth(w) {
    const side = appEl.classList.contains('side-collapsed') ? 0 : 252;
    const max = Math.max(EV_MIN, window.innerWidth - side - 440);   // 中欄至少留 440px
    w = Math.round(Math.min(Math.max(w, EV_MIN), max));
    appEl.style.setProperty('--ev-user', `${w}px`);
    return w;
  }
  const evResize = $('#evResize');
  evResize.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    evResize.setPointerCapture(e.pointerId);
    appEl.classList.add('resizing');
    let w = null;
    const move = (ev) => { w = setEvWidth(window.innerWidth - ev.clientX); };
    const up = () => {
      evResize.removeEventListener('pointermove', move);
      evResize.removeEventListener('pointerup', up);
      appEl.classList.remove('resizing');
      if (w) setPref('evw', String(w));
    };
    evResize.addEventListener('pointermove', move);
    evResize.addEventListener('pointerup', up);
  });
  evResize.addEventListener('dblclick', () => { appEl.style.removeProperty('--ev-user'); setPref('evw', ''); });
  window.addEventListener('resize', () => { const w = +pref('evw', 0); if (w) setEvWidth(w); });
  if (pref('side', 'open') === 'closed') appEl.classList.add('side-collapsed');
  if (+pref('evw', 0)) setEvWidth(+pref('evw', 0));

  $('#newTopic').addEventListener('click', newTopic);
  $('#topicTitle').addEventListener('click', async () => {
    if (!state.topic) return;
    const title = prompt('研究題目名稱', state.topic.title);
    if (!title || title === state.topic.title) return;
    state.topic = await api(`/api/topics/${state.topic.id}`, { title });
    await loadTopics();
  });
  $('#exportBtn').addEventListener('click', async () => {
    if (!state.topic) return;
    if (window.pywebview?.api?.export) {          // 桌面 app：跳「另存新檔」
      try {
        const path = await window.pywebview.api.export(state.topic.id);
        if (path) alert(`筆記已存到：\n${path}`);
      } catch (ex) { alert(`匯出失敗：${ex.message || ex}`); }
      return;
    }
    location.href = `/api/topics/${state.topic.id}/export`;
  });
  $('#evToggle').addEventListener('click', () => {
    const app = document.getElementById('app');
    app.classList.toggle('no-evidence');
    $('#evToggle').classList.toggle('on', !app.classList.contains('no-evidence'));
  });
  $('#themeBtn').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    setPref('theme', root.dataset.theme);
  });
  $('#pickerBtn').addEventListener('click', () => togglePicker());
  $('#wsBtn').addEventListener('click', () => toggleWs());
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('#ws')) toggleWs(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') toggleWs(false); });
  $('#effort').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    state.effort = b.dataset.v; setPref('effort', state.effort); renderPicker();
  });
  $('#scope').addEventListener('change', (e) => { state.scope = e.target.value; setPref('scope', state.scope); });
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('#picker')) togglePicker(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') togglePicker(false); });
  $('#input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit(); }
  });
  $('#composer').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (state.job) { await api(`/api/jobs/${state.job}/cancel`, {}).catch(() => {}); return; }
    ask();
  });

  // ── 視窗報到：開著就讓伺服器知道；伺服器不在了就明白說，不要讓畫面卡住 ──
  function presence() {
    const es = new EventSource('/api/presence');
    es.onerror = async () => {
      try { await fetch('/api/status', { cache: 'no-store' }); } catch {
        es.close();
        document.body.insertAdjacentHTML('beforeend', `<div class="closed"><div>
          <h2>卷證已經關閉</h2><p>請重新點桌面上的「卷證」圖示。已完成的問答都存著，重開後還在。</p>
          <button onclick="location.reload()">重新連線</button></div></div>`);
      }
    };
  }

  // ── 啟動 ──
  (async () => {
    presence();
    const theme = pref('theme', '');
    if (theme) document.documentElement.dataset.theme = theme;
    await refreshStatus();
    refreshUsage();                                   // 不等它（約 2 秒），先把畫面畫出來
    setInterval(() => { if (!state.job) refreshUsage(); }, 180000);
    await loadTopics();
    const busy = state.status.busy;
    const want = busy?.topic || pref('topic', '');
    const first = state.topics.find((t) => t.id === want) || state.topics[0];
    if (first) await openTopic(first.id); else { renderThread(); renderEvidence(); }
    if (busy && state.topic?.id === busy.topic) {
      const running = [...state.topic.turns].reverse().find((t) => t.status === 'running');
      if (running) follow(busy.job, running.id);
    }
    setInterval(() => { if (!state.job) refreshStatus().catch(() => {}); }, 60000);
  })().catch((ex) => { $('#thread').innerHTML = `<div class="err">連不上卷證伺服器：${esc(ex.message)}</div>`; });
})();
