// Milestone flow canvas: Goal -> Tech Lead -> tasks -> integration -> PR.
// Pan by dragging the background, zoom with the wheel, drag nodes (positions are remembered in this
// browser), click a node for details. Refreshes itself every 15 s without moving anything.
(function () {
  const root = document.getElementById('flow');
  if (!root) return;
  const src = root.dataset.src, key = 'flow:' + root.dataset.key;
  const COLW = 290, ROWH = 128, NW = 228;
  const ICON = {goal: '🎯', lead: '🧠', integration: '🔀', pr: '📬'};
  const BICON = {done: '✅', running: '⚙️', review: '🔍', revising: '✏️', waiting: '⏳', blocked: '⛔', cancelled: '✖️'};
  const canvas = root.querySelector('.canvas'), svg = root.querySelector('svg.edges'), panel = document.getElementById('panel');
  let data = null, view = {x: 30, y: 30, k: 1}, pos = load(), selected = null, showAll = false, fitted = false;

  function load() { try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { return {}; } }
  function save() { try { localStorage.setItem(key, JSON.stringify(pos)); } catch (e) {} }
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function place(n) { return pos[n.id] || [n.col * COLW, n.row * ROWH]; }
  function apply() { canvas.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.k})`; }

  function chip(text, cls) { return el('span', 'chip ' + (cls || ''), text); }
  function render() {
    canvas.querySelectorAll('.fnode').forEach(n => n.remove());
    const nodes = {};
    for (const n of data.nodes) {
      const d = el('div', `fnode k-${n.type}` + (n.bucket ? ` b-${n.bucket}` : '') + (n.running ? ' live' : '') + (selected === n.id ? ' sel' : ''));
      d.dataset.id = n.id;
      const [x, y] = place(n); d.style.left = x + 'px'; d.style.top = y + 'px';
      d.append(el('div', 'ico', n.type === 'task' ? (BICON[n.bucket] || '•') : ICON[n.type]));
      const bd = el('div', 'bd');
      bd.append(el('div', 't', n.title), el('div', 's', n.text || ''));
      const chips = el('div', 'chips');
      if (n.type === 'task') {
        chips.append(chip(n.status, {done: 'ok', blocked: 'bad', revising: 'warn', review: 'review', running: 'info'}[n.bucket]));
        if (n.running) chips.append(chip((n.running_purpose === 'review' ? '🔍 ' : '⚙️ ') + n.running, 'info'));
        else if (n.assignee && n.bucket !== 'done') chips.append(chip(n.assignee));
        if (n.revisions) chips.append(chip('sửa ' + n.revisions, 'warn'));
        if (n.pr_url) chips.append(chip('PR', 'ok'));
      } else if (n.type === 'goal') chips.append(chip(n.status));
      else if (n.type === 'lead') { if (n.agent) chips.append(chip(n.agent, n.running ? 'info' : '')); }
      else if (n.type === 'integration') chips.append(chip(n.status, n.pct === 100 ? 'ok' : ''), chip(n.pct + '%'));
      bd.append(chips); d.append(bd);
      if (n.type !== 'goal') d.append(el('span', 'port in'));
      if (n.type !== 'pr') d.append(el('span', 'port out'));
      canvas.append(d); nodes[n.id] = d;
    }
    drawEdges();
  }

  function drawEdges() {
    const byId = Object.fromEntries(data.nodes.map(n => [n.id, n]));
    let maxX = 0, maxY = 0, html = '';
    for (const e of data.edges) {
      const a = byId[e.from], b = byId[e.to]; if (!a || !b) continue;
      const da = canvas.querySelector(`.fnode[data-id="${a.id}"]`), db = canvas.querySelector(`.fnode[data-id="${b.id}"]`);
      const [ax, ay] = place(a), [bx, by] = place(b);
      const y1 = ay + (da ? da.offsetHeight / 2 : 44), y2 = by + (db ? db.offsetHeight / 2 : 44);
      const x1 = ax + NW, x2 = bx, dx = Math.max(40, Math.abs(x2 - x1) / 2);
      const cls = b.running || (a.type === 'lead' && a.running) ? 'active' : (a.bucket === 'done' ? 'done' : '');
      html += `<path class="${cls}" d="M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}"/>`;
      maxX = Math.max(maxX, x1, x2); maxY = Math.max(maxY, y1, y2);
    }
    svg.setAttribute('width', maxX + 300); svg.setAttribute('height', maxY + 200);
    svg.innerHTML = html;
  }

  function fit() {
    if (!data || !data.nodes.length) return;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const n of data.nodes) { const [x, y] = place(n); x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + NW); y1 = Math.max(y1, y + 100); }
    const W = root.clientWidth, H = root.clientHeight;
    view.k = Math.max(0.3, Math.min(1.2, Math.min((W - 60) / (x1 - x0), (H - 60) / (y1 - y0))));
    view.x = (W - (x1 - x0) * view.k) / 2 - x0 * view.k; view.y = (H - (y1 - y0) * view.k) / 2 - y0 * view.k;
    apply();
  }

  // ---------------------------------------------------------------- side panel
  function kv(pairs) { const g = el('div', 'kv'); for (const [k, v] of pairs) if (v) g.append(el('div', '', k), el('div', '', v)); return g; }
  async function show(id) {
    selected = id;
    canvas.querySelectorAll('.fnode').forEach(d => d.classList.toggle('sel', d.dataset.id === id));
    const n = data.nodes.find(x => x.id === id); if (!n || !panel) return;
    panel.innerHTML = '';
    panel.append(el('h3', '', (n.type === 'task' ? (BICON[n.bucket] || '') : ICON[n.type]) + ' ' + n.title));
    if (n.type !== 'task') {
      panel.append(el('p', 'muted small', n.text || ''));
      if (n.url) { const a = el('a', '', 'Mở PR trên GitHub ↗'); a.href = n.url; a.target = '_blank'; a.rel = 'noopener'; panel.append(a); }
      return;
    }
    panel.append(el('div', 'muted small', n.text));
    panel.append(kv([['Trạng thái', n.status], ['Người làm', n.assignee], ['Reviewer', n.reviewer], ['Lần sửa', String(n.revisions || 0)],
                     ['Đang chạy', n.running ? `${n.running} (${n.running_purpose})` : '']]));
    if (n.note) { panel.append(el('div', 'muted small', n.bucket === 'blocked' ? 'Lý do bị chặn' : 'Cần sửa')); panel.append(el('pre', '', n.note)); }
    const links = el('div', 'row');
    const a = el('a', '', 'Chi tiết task, diff, log →'); a.href = '/tasks/' + n.task_id; links.append(a);
    if (n.pr_url) { const p = el('a', '', 'Step PR ↗'); p.href = n.pr_url; p.target = '_blank'; p.rel = 'noopener'; links.append(p); }
    panel.append(links);
    try {
      const r = await fetch('/api/tasks/' + n.task_id, {credentials: 'same-origin'});
      if (!r.ok || selected !== id) return;
      const b = await r.json(), rv = (b.reviews || []).slice(-1)[0];
      if (rv) { panel.append(el('div', 'muted small', `Review mới nhất · ${rv.reviewer} · ${rv.verdict}`)); panel.append(el('pre', '', rv.body)); }
      else if (b.task && b.task.description) { panel.append(el('div', 'muted small', 'Mô tả')); panel.append(el('pre', '', b.task.description)); }
    } catch (e) {}
  }

  // ---------------------------------------------------------------- interaction
  let drag = null;
  root.addEventListener('pointerdown', ev => {
    if (ev.target.closest('.tools')) return;
    const node = ev.target.closest('.fnode');
    drag = {node, sx: ev.clientX, sy: ev.clientY, moved: false, vx: view.x, vy: view.y};
    if (node) { const n = data.nodes.find(x => x.id === node.dataset.id); drag.start = place(n); drag.n = n; }
    else root.classList.add('panning');
    root.setPointerCapture(ev.pointerId);
  });
  root.addEventListener('pointermove', ev => {
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.node) {
      const x = drag.start[0] + dx / view.k, y = drag.start[1] + dy / view.k;
      pos[drag.n.id] = [x, y]; drag.node.style.left = x + 'px'; drag.node.style.top = y + 'px'; drawEdges();
    } else { view.x = drag.vx + dx; view.y = drag.vy + dy; apply(); }
  });
  root.addEventListener('pointerup', () => {
    if (drag && drag.node) { if (drag.moved) save(); else show(drag.node.dataset.id); }
    drag = null; root.classList.remove('panning');
  });
  root.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = root.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const k = Math.max(0.25, Math.min(2, view.k * (ev.deltaY < 0 ? 1.1 : 1 / 1.1)));
    view.x = mx - (mx - view.x) * k / view.k; view.y = my - (my - view.y) * k / view.k; view.k = k; apply();
  }, {passive: false});
  const tool = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
  tool('fit', fit);
  tool('zin', () => { view.k = Math.min(2, view.k * 1.2); apply(); });
  tool('zout', () => { view.k = Math.max(0.25, view.k / 1.2); apply(); });
  tool('relayout', () => { pos = {}; save(); render(); fit(); });
  tool('showall', ev => { showAll = !showAll; ev.target.textContent = showAll ? 'Ẩn task đã huỷ' : 'Hiện task đã huỷ'; refresh(); });

  async function refresh() {
    if (document.hidden && data) return;
    try {
      const r = await fetch(src + (showAll ? (src.includes('?') ? '&' : '?') + 'all=1' : ''), {credentials: 'same-origin'});
      if (r.redirected) { location.reload(); return; }
      if (!r.ok) return;
      data = await r.json();
      if (!data.nodes.length) { root.querySelector('.hint').textContent = 'Milestone chưa có gì để vẽ.'; return; }
      render(); if (!fitted) { fit(); fitted = true; }
      if (selected) show(selected);
      const at = document.getElementById('live-at');
      if (at) at.textContent = 'cập nhật ' + new Date().toLocaleTimeString('vi-VN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    } catch (e) {}
  }
  refresh(); setInterval(refresh, 15000);
  window.addEventListener('resize', () => { if (!Object.keys(pos).length) fit(); });
})();
