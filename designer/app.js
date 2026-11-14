/* LeafBus designer: editor and connection view. Model: model.js (window.LeafModel). */
(function () {
  'use strict';
  const M = window.LeafModel;
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const NS = 'http://www.w3.org/2000/svg';
  const svgEl = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  };
  const cv = $('#cv'), stage = $('#stage');
  const G = {grid: $('#g-grid'), panels: $('#g-panels'), over: $('#g-over'), ghost: $('#g-ghost')};
  const GROUP_COLORS = ['#4aa3ff', '#ff7a59', '#3ccf8e', '#c07cff', '#ffd23f', '#ff5da2'];
  const STORE = 'leafbus.designer.v1';

  // ---- state ----------------------------------------------------------------
  const S = {
    pieces: [], sel: new Set(), tool: 'hex', view: 'design',
    cam: {x: 0, y: 0, k: 1.2},              // centre (mm), pixels per mm
    prefer: 'shallow', power: Object.assign({}, M.DEFAULT_POWER),
    ctl: null, psus: [],                     // overrides: {key, piece}
    showExtras: true, pick: null,
    dims: true, units: 'cm',                // sizes overlay; cm or inches
    undo: [], redo: [],
    an: null, plan: null,
  };

  // ---- history, persistence ---------------------------------------------------------
  const snapshot = () => JSON.stringify({p: S.pieces, c: S.ctl, s: S.psus});
  function restore(snap) {
    const d = JSON.parse(snap);
    S.pieces = d.p; S.ctl = d.c; S.psus = d.s || [];
    M.reserveIds(S.pieces);
    for (const id of [...S.sel]) if (!S.pieces.some(p => p.id === id)) S.sel.delete(id);
  }
  /** Every change to the layout goes through here: undo, autosave, refresh. */
  function commit(fn) {
    const before = snapshot();
    fn();
    if (snapshot() === before) return;
    S.undo.push(before);
    if (S.undo.length > 200) S.undo.shift();
    S.redo = [];
    changed();
  }
  function changed() {
    // overrides that no longer point at a panel edge are dropped
    const keys = new Set();
    for (const p of S.pieces) for (const e of M.boundary(p)) keys.add(e.key + '@' + p.id);
    if (S.ctl && !keys.has(S.ctl.key + '@' + S.ctl.piece)) S.ctl = null;
    S.psus = S.psus.filter(w => keys.has(w.key + '@' + w.piece));
    refresh();
    try {
      localStorage.setItem(STORE, JSON.stringify({pieces: S.pieces, ctl: S.ctl, psus: S.psus,
        power: S.power, prefer: S.prefer, view: S.view, dims: S.dims, units: S.units}));
    } catch (e) { /* private window or storage blocked: nothing to keep */ }
  }
  function undo() { if (!S.undo.length) return; S.redo.push(snapshot()); restore(S.undo.pop()); changed(); }
  function redo() { if (!S.redo.length) return; S.undo.push(snapshot()); restore(S.redo.pop()); changed(); }

  /** Compact form for files and links: panels as text, overrides by panel index. */
  function exportDesign() {
    const idx = new Map(S.pieces.map((p, i) => [p.id, i]));
    const ref = w => w && idx.has(w.piece) ? {key: w.key, i: idx.get(w.piece)} : null;
    return {format: 'leafbus-design', version: 1, panels: M.encodeAll(S.pieces),
            controller: ref(S.ctl), psus: S.psus.map(ref).filter(Boolean),
            power: S.power, prefer: S.prefer};
  }
  function importDesign(d) {
    if (!d || d.format !== 'leafbus-design') throw new Error('not a LeafBus design file');
    const ps = M.decodeAll(d.panels || '');
    const ref = r => r && ps[r.i] ? {key: r.key, piece: ps[r.i].id} : null;
    commit(() => {
      S.pieces = ps; S.sel.clear();
      S.ctl = ref(d.controller); S.psus = (d.psus || []).map(ref).filter(Boolean);
      if (d.power) S.power = Object.assign({}, M.DEFAULT_POWER, d.power);
      if (d.prefer) S.prefer = d.prefer;
    });
    syncInputs();
  }
  function shareHash() {
    const d = exportDesign();
    const parts = ['d=' + encodeURIComponent(d.panels)];
    if (d.controller) parts.push('c=' + encodeURIComponent(d.controller.key + '~' + d.controller.i));
    if (d.psus.length) parts.push('p=' + encodeURIComponent(d.psus.map(w => w.key + '~' + w.i).join('/')));
    return '#' + parts.join('&');
  }
  function fromHash(h) {
    const q = {};
    for (const kv of h.replace(/^#/, '').split('&')) {
      const i = kv.indexOf('=');
      if (i > 0) q[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
    }
    if (!q.d) return false;
    const ref = s => { const [key, i] = s.split('~'); return {key, i: +i}; };
    importDesign({format: 'leafbus-design', panels: q.d, controller: q.c ? ref(q.c) : null,
                  psus: q.p ? q.p.split('/').map(ref) : []});
    return true;
  }

  // ---- analysis -----------------------------------------------------------------------
  function refresh() {
    S.an = M.analyse(S.pieces);
    S.plan = S.pieces.length ? M.plan(S.pieces, {controller: S.ctl, psus: S.psus, prefer: S.prefer, power: S.power}) : null;
    render();
    renderSide();
  }

  // ---- camera ----------------------------------------------------------------------------
  function viewBox() {
    const w = stage.clientWidth, h = stage.clientHeight, k = S.cam.k;
    return {x: S.cam.x - w / 2 / k, y: S.cam.y - h / 2 / k, w: w / k, h: h / k};
  }
  /** Markers and labels grow when zoomed out so they stay readable. */
  const uiScale = () => Math.max(1, Math.min(4, 0.9 / S.cam.k));
  let drawnScale = 1;
  function applyCamera() {
    const v = viewBox();
    cv.setAttribute('viewBox', [v.x, v.y, v.w, v.h].join(' '));
    drawGrid(v);
    if (Math.abs(uiScale() - drawnScale) > 0.05) render();
  }
  function toWorld(ev) {
    const pt = cv.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    const w = pt.matrixTransform(cv.getScreenCTM().inverse());
    return [w.x, w.y];
  }
  function zoomAt(f, cx, cy) {
    const k0 = S.cam.k, k1 = Math.max(0.08, Math.min(8, k0 * f));
    if (cx !== undefined) {
      S.cam.x = cx - (cx - S.cam.x) * k0 / k1;
      S.cam.y = cy - (cy - S.cam.y) * k0 / k1;
    }
    S.cam.k = k1;
    applyCamera();
  }
  function fit() {
    const b = M.bounds(S.pieces);
    if (!b) { S.cam = {x: 0, y: 0, k: 1.2}; applyCamera(); return; }
    const w = stage.clientWidth, h = stage.clientHeight, m = 90;
    S.cam.x = (b.x0 + b.x1) / 2; S.cam.y = (b.y0 + b.y1) / 2;
    S.cam.k = Math.max(0.08, Math.min(3, Math.min(w / (b.w + 2 * m), h / (b.h + 2 * m))));
    applyCamera();
  }

  function drawGrid(v) {
    G.grid.innerHTML = '';
    if (S.cam.k * M.L < 9) return;
    const L = M.L, H = M.H, x0 = v.x, x1 = v.x + v.w;
    const bmin = Math.floor(-(v.y + v.h) / H) - 1, bmax = Math.ceil(-v.y / H) + 1;
    let d = '';
    for (let b = bmin; b <= bmax; b++) d += 'M' + x0 + ' ' + (-b * H) + 'H' + x1;
    const amin = Math.floor(x0 / L - bmax / 2) - 1, amax = Math.ceil(x1 / L - bmin / 2) + 1;
    for (let a = amin; a <= amax; a++) {
      const p = M.P(a, bmin), q = M.P(a, bmax);
      d += 'M' + p[0] + ' ' + p[1] + 'L' + q[0] + ' ' + q[1];
    }
    const cmin = Math.floor(x0 / L + bmin / 2) - 1, cmax = Math.ceil(x1 / L + bmax / 2) + 1;
    for (let c = cmin; c <= cmax; c++) {
      const p = M.P(c - bmin, bmin), q = M.P(c - bmax, bmax);
      d += 'M' + p[0] + ' ' + p[1] + 'L' + q[0] + ' ' + q[1];
    }
    svgEl('path', {d, stroke: 'var(--grid)', fill: 'none', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke'}, G.grid);
  }

  // ---- drawing ---------------------------------------------------------------------------------
  const pts = p => p.v.map(v => M.P(v[0], v[1]).map(n => n.toFixed(2)).join(',')).join(' ');
  const typeFill = t => 'var(--' + t + ')';
  const depthFill = (d, max) => d === undefined ? '#555' : 'hsl(' + (140 - 140 * d / Math.max(1, max)) + ',62%,47%)';

  function render() {
    G.panels.innerHTML = '';
    G.over.innerHTML = '';
    $('#empty').style.display = S.pieces.length ? 'none' : '';
    const plan = S.plan, conn = S.view === 'connections' && plan;
    const bad = new Set(S.an ? S.an.overlaps : []);
    const island = new Set(plan ? plan.unreached : []);
    const group = new Map();
    if (conn && plan.psus.length > 1) plan.psus.forEach((s, i) => s.panels.forEach(id => group.set(id, i)));
    const maxD = conn ? plan.stats.max : 0;
    const u = drawnScale = uiScale();
    for (const p of S.pieces) {
      const cls = ['pc'];
      if (S.sel.has(p.id)) cls.push('sel');
      if (bad.has(p.id)) cls.push('bad');
      else if (island.has(p.id)) cls.push('island');
      const fill = conn ? depthFill(plan.depth.get(p.id), maxD) : typeFill(p.type);
      const style = 'fill:' + fill + (group.has(p.id) && !S.sel.has(p.id)
        ? ';stroke:' + GROUP_COLORS[group.get(p.id) % GROUP_COLORS.length] + ';stroke-width:2.5' : '');
      svgEl('polygon', {points: pts(p), class: cls.join(' '), style, 'data-id': p.id}, G.panels);
      if (conn && plan.depth.has(p.id)) {
        const c = M.centre(p), t = svgEl('text', {x: c[0], y: c[1], class: 'lbl',
          style: 'font-size:' + Math.min(p.type === 'mini' ? 14 : 34, (p.type === 'mini' ? 11 : 17) * u) + 'px'}, G.panels);
        t.textContent = plan.depth.get(p.id);
      }
    }
    if (conn) drawConnections(plan);
    if (S.dims && S.pieces.length) drawDims();
    if (S.pick) drawPickDots();
    renderStart();          // follows the selection
  }

  // ---- sizes and the start panel --------------------------------------------------------------
  const fmtLen = mm => S.units === 'in' ? (mm / 25.4).toFixed(2) + ' in' : (mm / 10).toFixed(1) + ' cm';

  /**
   * Where to start on the wall: one selected panel, or else the panel whose
   * centre is nearest the middle of the design. Returns the panel, its centre
   * and the distances from that centre to the outer edges of the design.
   */
  function startPanel() {
    const b = M.bounds(S.pieces);
    if (!b) return null;
    let p = S.sel.size === 1 ? S.pieces.find(x => S.sel.has(x.id)) : null;
    const chosen = !!p;
    if (!p) {
      const mx = (b.x0 + b.x1) / 2, my = (b.y0 + b.y1) / 2;
      let bd = Infinity;
      for (const x of S.pieces) {
        const c = M.centre(x), d = Math.hypot(c[0] - mx, c[1] - my);
        if (d < bd - 1e-6) { bd = d; p = x; }
      }
    }
    const c = M.centre(p);
    return {piece: p, chosen, c, b, left: c[0] - b.x0, right: b.x1 - c[0], up: c[1] - b.y0, down: b.y1 - c[1]};
  }

  function dimLabel(x, y, text, rotate) {
    const t = svgEl('text', {x, y, class: 'dimlbl', style: 'font-size:' + 13 * drawnScale + 'px'}, G.over);
    if (rotate) t.setAttribute('transform', 'rotate(-90 ' + x + ' ' + y + ')');
    t.textContent = text;
  }
  function drawDims() {
    const st = startPanel(), b = st.b, u = drawnScale, off = 34 * u, tick = 8 * u;
    const line = (x1, y1, x2, y2, cls) => svgEl('line', {x1, y1, x2, y2, class: 'dim' + (cls ? ' ' + cls : '')}, G.over);
    svgEl('rect', {x: b.x0, y: b.y0, width: b.w, height: b.h, class: 'dim box'}, G.over);
    // overall width above, overall height to the right
    const yw = b.y0 - off, xh = b.x1 + off;
    line(b.x0, yw, b.x1, yw);
    line(b.x0, yw - tick, b.x0, yw + tick); line(b.x1, yw - tick, b.x1, yw + tick);
    line(b.x0, b.y0, b.x0, yw, 'thin'); line(b.x1, b.y0, b.x1, yw, 'thin');
    dimLabel((b.x0 + b.x1) / 2, yw - 12 * u, 'width ' + fmtLen(b.w));
    line(xh, b.y0, xh, b.y1);
    line(xh - tick, b.y0, xh + tick, b.y0); line(xh - tick, b.y1, xh + tick, b.y1);
    line(b.x1, b.y0, xh, b.y0, 'thin'); line(b.x1, b.y1, xh, b.y1, 'thin');
    dimLabel(xh + 14 * u, (b.y0 + b.y1) / 2, 'height ' + fmtLen(b.h), true);
    // from the start panel's centre to each edge
    const [cx, cy] = st.c;
    line(b.x0, cy, b.x1, cy, 'thin');
    line(cx, b.y0, cx, b.y1, 'thin');
    const room = 32 * u;   // label only where there is room for it
    if (st.left > room) dimLabel((b.x0 + cx) / 2, cy - 10 * u, '← ' + fmtLen(st.left));
    if (st.right > room) dimLabel((cx + b.x1) / 2, cy - 10 * u, fmtLen(st.right) + ' →');
    if (st.up > room) dimLabel(cx + 12 * u, (b.y0 + cy) / 2, '↑ ' + fmtLen(st.up), true);
    if (st.down > room) dimLabel(cx + 12 * u, (cy + b.y1) / 2, fmtLen(st.down) + ' ↓', true);
    // crosshair on the start panel
    svgEl('polygon', {points: pts(st.piece), class: 'start'}, G.over);
    svgEl('circle', {cx, cy, r: 9 * u, class: 'start'}, G.over);
    line(cx - 14 * u, cy, cx + 14 * u, cy); line(cx, cy - 14 * u, cx, cy + 14 * u);
    dimLabel(cx, cy + 22 * u, st.chosen ? 'measured from here' : 'start here');
  }

  function edgeAngle(e) {
    const a = M.P(e.a[0], e.a[1]), b = M.P(e.b[0], e.b[1]);
    return Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI;
  }
  function linkerRect(e, cls) {
    svgEl('rect', {x: -14, y: -3.5, width: 28, height: 7, rx: 2, class: cls,
      transform: 'translate(' + e.mid[0] + ' ' + e.mid[1] + ') rotate(' + edgeAngle(e) + ')'}, G.over);
  }
  function marker(e, color, icon, label) {
    const u = drawnScale, d = 22 * u, x = e.mid[0] + e.normal[0] * d, y = e.mid[1] + e.normal[1] * d;
    svgEl('line', {x1: e.mid[0], y1: e.mid[1], x2: x, y2: y, stroke: color, 'stroke-width': 3,
      'vector-effect': 'non-scaling-stroke'}, G.over);
    svgEl('circle', {cx: x, cy: y, r: 12 * u, fill: color, stroke: '#0b0d11', 'stroke-width': 1.5,
      'vector-effect': 'non-scaling-stroke'}, G.over);
    svgEl('use', {href: '#' + icon, x: x - 8 * u, y: y - 8 * u, width: 16 * u, height: 16 * u, style: 'color:#fff'}, G.over);
    if (label) {
      const t = svgEl('text', {x: x + 13 * u, y: y - 10 * u, class: 'lbl', style: 'font-size:' + 11 * u + 'px'}, G.over);
      t.textContent = label;
    }
  }
  function drawConnections(plan) {
    const byId = S.an.byId;
    for (const t of plan.tree) {
      const a = M.centre(byId.get(t.parent)), b = M.centre(byId.get(t.child)), m = t.edge.mid;
      svgEl('path', {d: 'M' + a[0] + ' ' + a[1] + 'L' + m[0] + ' ' + m[1] + 'L' + b[0] + ' ' + b[1],
        class: 'link', 'marker-end': 'url(#arrow)'}, G.over);
    }
    for (const t of plan.tree) linkerRect(t.edge, 'linker');
    if (S.showExtras) for (const x of plan.extras) linkerRect(x.edge, 'extra');
    if (plan.controller) marker(plan.controller.edge, '#2fb67a', 'i-plug');
    plan.psus.forEach((s, i) => marker(s.edge, GROUP_COLORS[i % GROUP_COLORS.length], 'i-bolt',
      plan.psus.length > 1 ? String(i + 1) : ''));
  }
  function pickEdges() {
    if (!S.plan || !S.an) return [];
    const main = new Set(S.an.components[0] || []);
    return S.an.free.filter(e => main.has(e.piece));
  }
  function drawPickDots() {
    for (const e of pickEdges()) {
      svgEl('circle', {cx: e.mid[0], cy: e.mid[1], r: 7 * drawnScale, class: 'free', 'data-key': e.key, 'data-piece': e.piece}, G.over);
    }
  }

  // ---- side panel ----------------------------------------------------------------------------------
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const fmtW = w => (Math.round(w * 10) / 10) + ' W';
  function renderStart() {
    const st = startPanel(), box = $('#startinfo');
    $$('#units button').forEach(x => x.classList.toggle('on', x.dataset.u === S.units));
    $('#dims').classList.toggle('on', S.dims);
    if (!st) { box.innerHTML = ''; return; }
    const name = M.TYPES[st.piece.type].label;
    box.innerHTML = '<div class="startinfo"><b>' + (st.chosen ? 'Measured from the selected ' + name
        : 'Start with the ' + name + ' nearest the middle') + '</b> — from its centre to the edges of the finished design:' +
      '<div class="grid4"><span>left</span><span>' + fmtLen(st.left) + '</span><span>right</span><span>' + fmtLen(st.right) +
      '</span><span>up</span><span>' + fmtLen(st.up) + '</span><span>down</span><span>' + fmtLen(st.down) + '</span></div>' +
      '<div class="small faint" style="margin-top:4px">' + (st.chosen ? 'Clear the selection (Esc) to go back to the middle panel.'
        : 'Select one panel to measure from it instead.') + '</div></div>';
  }
  function renderSide() {
    const n = {hex: 0, tri: 0, mini: 0};
    for (const p of S.pieces) n[p.type]++;
    for (const t in n) $('#n-' + t).textContent = n[t];
    const b = M.bounds(S.pieces), plan = S.plan;
    const total = M.power(S.pieces, S.power);
    $('#summary').innerHTML = S.pieces.length ? [
      ['Panels', S.pieces.length],
      ['Size', fmtLen(b.w) + ' wide × ' + fmtLen(b.h) + ' high'],
      ['Power', fmtW(total) + ' at full brightness'],
      ['Linkers', plan ? plan.tree.length + ' needed, ' + plan.extras.length + ' optional' : '—'],
    ].map(([k, v]) => '<b>' + k + '</b><span>' + v + '</span>').join('') : '<span class="faint">No panels yet.</span>';

    const probs = plan ? plan.problems : [];
    $('#problems').innerHTML = probs.map(p => '<div class="prob">' + esc(p.text) + '</div>').join('') +
      (plan && !probs.length ? '<div class="good">All ' + S.pieces.length + ' panels can be reached and powered.</div>' : '');

    // controller
    const c = plan && plan.controller;
    $('#ctl').innerHTML = !c ? '<span class="faint">Add panels first.</span>' :
      '<div class="kv"><b>Farthest panel</b><span>' + c.max + ' hop' + (c.max === 1 ? '' : 's') +
      '</span><b>Average</b><span>' + c.avg.toFixed(1) + ' hops</span></div>' +
      '<div class="row small" style="margin-top:6px"><span class="' + (c.forced ? 'muted' : 'faint') + '">' +
      (c.forced ? 'Placed by you' + (plan.alternatives[0] && plan.alternatives[0].max < c.max
        ? ' — the best spot reaches every panel in ' + plan.alternatives[0].max : '')
        : 'Best spot (' + (S.prefer === 'bottom' ? 'lowest edge, then fewest hops' : 'fewest hops') + ')') +
      '</span>' + (c.forced ? '<button class="btn sm" id="ctl-auto">Auto</button>' : '') + '</div>';
    const auto = $('#ctl-auto');
    if (auto) auto.onclick = () => commit(() => { S.ctl = null; });
    $$('#prefer button').forEach(x => x.classList.toggle('on', x.dataset.p === S.prefer));
    const alts = plan ? plan.alternatives : [];
    $('#alts').innerHTML = alts.length > 1 ? '<div class="small faint" style="margin-bottom:4px">Good spots</div>' +
      alts.map((a, i) => '<button class="alt' + (c && c.edge.key === a.edge.key ? ' on' : '') + '" data-i="' + i + '">' +
        (i + 1) + '. farthest ' + a.max + ' hops<i>avg ' + a.avg.toFixed(1) + '</i></button>').join('') : '';
    $$('#alts .alt').forEach(btn => btn.onclick = () => {
      const a = alts[+btn.dataset.i];
      commit(() => { S.ctl = {key: a.edge.key, piece: a.piece}; });
      setView('connections');
    });

    // linkers
    $('#linkers').innerHTML = !plan ? '<span class="faint">—</span>' :
      '<div class="kv"><b>Bus</b><span>' + plan.tree.length + ' linkers (one per panel after the first)</span>' +
      '<b>Optional</b><span>' + plan.extras.length + ' more places where panels touch</span></div>';

    // power
    const pw = plan && plan.power;
    $('#psus').innerHTML = !pw ? '<span class="faint">—</span>' :
      '<div class="kv" style="margin-bottom:8px"><b>Total</b><span>' + fmtW(pw.total) + ' → ' + pw.needed + ' suppl' +
      (pw.needed === 1 ? 'y' : 'ies') + ' of ' + pw.perPsu + ' W</span><b>Farthest panel</b><span>' + pw.maxHops +
      ' hops from a supply</span></div>' +
      plan.psus.map((s, i) => {
        const pct = Math.min(100, s.load / pw.perPsu * 100);
        return '<div class="small" style="margin-bottom:8px"><span style="color:' + GROUP_COLORS[i % GROUP_COLORS.length] +
          '">●</span> Supply ' + (i + 1) + (s.forced ? ' (placed by you)' : '') + ': ' + fmtW(s.load) + ', ' + s.panels.length +
          ' panels<div class="bar"><i class="' + (s.load > pw.perPsu ? 'over' : '') + '" style="width:' + pct + '%"></i></div></div>';
      }).join('') +
      (plan.psus.some(s => s.forced) ? '<button class="btn sm" id="psu-auto">Auto</button>' : '');
    const pa = $('#psu-auto');
    if (pa) pa.onclick = () => commit(() => { S.psus = []; });

    $$('[data-pick]').forEach(b => {
      const on = S.pick === b.dataset.pick;
      b.textContent = on ? 'Done' : 'Place…';
      b.classList.toggle('primary', on);
      b.disabled = !S.pieces.length;
    });
    renderStart();
    $('#undo').disabled = !S.undo.length;
    $('#redo').disabled = !S.redo.length;
    hint();
  }

  const HINTS = {
    select: 'Click to select (Shift adds), drag to move. Drag empty space to pan, Shift+drag to select an area. R rotates, F mirrors, Del deletes.',
    hex: 'Click to place a Hexagon; hold and drag to place several. Right-click removes a panel.',
    tri: 'Click to place a Triangle — it points up or down depending on where in the grid you click.',
    mini: 'Click a grid triangle to fill it with a Mini Triangle.',
    erase: 'Click or drag over panels to remove them.',
  };
  function hint() {
    $('#hint').textContent = S.pick === 'controller' ? 'Click a dot on the outside edge to put the controller there. Esc cancels.'
      : S.pick === 'psu' ? 'Click a dot to add or move a power supply, click a supply\'s dot to remove it. Done or Esc when finished.'
      : HINTS[S.tool] + (S.view === 'connections' ? '  Numbers are hops from the controller.' : '');
  }

  function toast(msg, bad) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast on' + (bad ? ' bad' : '');
    clearTimeout(t._t);
    t._t = setTimeout(() => { t.className = 'toast'; }, bad ? 4200 : 2400);
  }
  function ask(title, body, ok) {
    return new Promise(res => {
      const d = $('#dlg');
      $('#dlgt').textContent = title;
      $('#dlgb').innerHTML = '';
      if (typeof body === 'string') $('#dlgb').innerHTML = body; else $('#dlgb').append(body);
      $('#dlgok').textContent = ok || 'OK';
      d.onclose = () => res(d.returnValue === 'ok');
      d.returnValue = '';
      d.showModal();
      const inp = $('#dlgb input');
      if (inp) setTimeout(() => { inp.focus(); inp.select(); }, 30);
    });
  }

  // ---- editing helpers -----------------------------------------------------------------------
  function ownerMap(exclude) {
    const m = new Map();
    for (const p of S.pieces) {
      if (exclude && exclude.has(p.id)) continue;
      for (const [q, r, s] of M.cells(p)) m.set(M.ckey(q, r, s), p.id);
    }
    return m;
  }
  const fits = (ps, owner) => ps.every(p => M.cells(p).every(([q, r, s]) => !owner.has(M.ckey(q, r, s))));
  function pieceAt(xy) {
    if (!S.an) return null;
    const [q, r, s] = M.cellAt(xy[0], xy[1]);
    const id = S.an.owner.get(M.ckey(q, r, s));
    return id === undefined ? null : id;
  }

  /** The panel of the current tool snapped to the grid under the pointer. */
  function snapped(type, x, y) {
    const [a, b] = M.toLattice(x, y);
    if (type === 'mini') { const [q, r, s] = M.cellAt(x, y); return M.make('mini', q, r, s === 1); }
    let best = null, bd = Infinity;
    const consider = (q, r, down, ca, cb) => {
      const c = M.P(ca, cb), d = Math.hypot(c[0] - x, c[1] - y);
      if (d < bd) { bd = d; best = [q, r, down]; }
    };
    if (type === 'hex') {
      for (let q = Math.floor(a) - 1; q <= Math.ceil(a) + 1; q++)
        for (let r = Math.floor(b) - 1; r <= Math.ceil(b) + 1; r++) consider(q, r, false, q, r);
      return M.make('hex', best[0], best[1]);
    }
    for (let q = Math.floor(a) - 2; q <= Math.ceil(a) + 1; q++) {
      for (let r = Math.floor(b) - 2; r <= Math.ceil(b) + 1; r++) {
        consider(q, r, false, q + 2 / 3, r + 2 / 3);
        consider(q, r, true, q + 4 / 3, r + 4 / 3);
      }
    }
    return M.make('tri', best[0], best[1], best[2]);
  }

  function drawGhost(ps, ok) {
    G.ghost.innerHTML = '';
    if (!ps || !ps.length) return;
    const g = svgEl('g', {class: 'ghost' + (ok ? '' : ' bad')}, G.ghost);
    for (const p of ps) svgEl('polygon', {points: pts(p), class: 'pc', style: 'fill:' + typeFill(p.type)}, g);
  }

  function selected() { return S.pieces.filter(p => S.sel.has(p.id)); }

  /** Replace the selected panels with transformed ones, if they still fit. */
  function transformSelection(fn, what) {
    let ids = S.sel.size ? S.sel : new Set(S.pieces.map(p => p.id));
    const sel = S.pieces.filter(p => ids.has(p.id));
    if (!sel.length) return;
    const moved = fn(sel), owner = ownerMap(ids);
    if (!fits(moved, owner)) { toast('No room to ' + what + ' here', true); return; }
    const byId = new Map(moved.map(p => [p.id, p]));
    commit(() => { S.pieces = S.pieces.map(p => byId.get(p.id) || p); });
  }

  function duplicate() {
    const sel = selected();
    if (!sel.length) return;
    const owner = ownerMap();
    for (let ring = 1; ring < 40; ring++) {
      for (const [dq, dr] of [[2 * ring, -ring], [ring, ring], [-ring, 2 * ring], [-2 * ring, ring], [-ring, -ring], [ring, -2 * ring]]) {
        const copy = M.translate(sel.map(p => M.clone(p)), dq, dr);
        if (fits(copy, owner)) {
          commit(() => { S.pieces = S.pieces.concat(copy); S.sel = new Set(copy.map(p => p.id)); });
          return;
        }
      }
    }
  }

  function setTool(t) {
    S.tool = t;
    $$('#tools [data-t]').forEach(b => b.classList.toggle('on', b.dataset.t === t));
    cv.setAttribute('class', t === 'select' ? 'pan' : 'place');
    drawGhost(null);
    hint();
  }
  function setView(v) {
    S.view = v;
    $$('#views button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
    render();
    hint();
  }
  function setPick(p) {
    S.pick = S.pick === p ? null : p;
    if (S.pick) setView('connections');
    cv.classList.toggle('pick', !!S.pick);
    render();
    renderSide();
  }

  function pickEdge(key, piece) {
    if (S.pick === 'controller') {
      commit(() => { S.ctl = {key, piece}; });
      S.pick = null;
      cv.classList.remove('pick');
      render(); renderSide();
      return;
    }
    // supplies: start from the current plan, then add / move / remove
    const plan = S.plan;
    if (plan.controller && key === plan.controller.edge.key) { toast('The controller sits there', true); return; }
    let list = S.psus.length ? S.psus.slice() : plan.psus.map(s => ({key: s.edge.key, piece: s.piece}));
    const at = list.findIndex(w => w.key === key);
    if (at >= 0) list.splice(at, 1);
    else if (list.length < plan.power.needed) list.push({key, piece});
    else {
      const e = S.an.edges.get(key)[0];
      let ni = 0, nd = Infinity;
      list.forEach((w, i) => {
        const x = S.an.edges.get(w.key)[0], d = Math.hypot(x.mid[0] - e.mid[0], x.mid[1] - e.mid[1]);
        if (d < nd) { nd = d; ni = i; }
      });
      list[ni] = {key, piece};
    }
    commit(() => { S.psus = list; });
  }

  // ---- pointer input ------------------------------------------------------------------------
  const drag = {mode: null};
  const touches = new Map();
  let spaceDown = false;

  cv.addEventListener('contextmenu', ev => ev.preventDefault());
  cv.addEventListener('pointerdown', ev => {
    cv.setPointerCapture(ev.pointerId);
    if (ev.pointerType === 'touch') {
      touches.set(ev.pointerId, [ev.clientX, ev.clientY]);
      if (touches.size === 2) { drag.mode = 'pinch'; drag.pinch = pinchState(); drawGhost(null); return; }
    }
    const xy = toWorld(ev);
    const dot = ev.target.closest && ev.target.closest('.free');
    if (S.pick && dot) { pickEdge(dot.dataset.key, +dot.dataset.piece); return; }
    if (ev.button === 1 || spaceDown) { startPan(ev); return; }
    const hit = pieceAt(xy);
    if (ev.button === 2) {
      if (hit !== null) commit(() => { S.pieces = S.pieces.filter(p => p.id !== hit); S.sel.delete(hit); });
      return;
    }
    if (S.pick) { startPan(ev); return; }
    if (S.tool === 'erase') {
      drag.mode = 'erase'; drag.batch = snapshot(); drag.any = false;
      eraseAt(xy);
      return;
    }
    if (S.tool === 'select') {
      if (hit !== null) {
        if (ev.shiftKey) {
          S.sel.has(hit) ? S.sel.delete(hit) : S.sel.add(hit);
          render();
          return;
        }
        if (!S.sel.has(hit)) { S.sel = new Set([hit]); render(); }
        drag.mode = 'move'; drag.start = xy; drag.d = [0, 0]; drag.ok = true;
        return;
      }
      if (ev.shiftKey) {
        drag.mode = 'marquee'; drag.start = xy;
        return;
      }
      if (S.sel.size) { S.sel.clear(); render(); }
      startPan(ev);
      return;
    }
    // placing
    drag.mode = 'place'; drag.batch = snapshot(); drag.any = false;
    placeAt(xy);
  });

  function startPan(ev) {
    drag.mode = 'pan'; drag.sx = ev.clientX; drag.sy = ev.clientY; drag.cx = S.cam.x; drag.cy = S.cam.y;
    cv.classList.add('panning');
  }
  function pinchState() {
    const [a, b] = [...touches.values()];
    return {d: Math.hypot(a[0] - b[0], a[1] - b[1]), mx: (a[0] + b[0]) / 2, my: (a[1] + b[1]) / 2,
            k: S.cam.k, cx: S.cam.x, cy: S.cam.y};
  }

  /** Placing and erasing while dragging add up to one undo step. */
  function batched(fn) {
    fn();
    drag.any = true;
    S.redo = [];
    changed();
  }
  function placeAt(xy) {
    const p = snapped(S.tool, xy[0], xy[1]);
    if (!fits([p], S.an ? S.an.owner : ownerMap())) return;
    batched(() => { S.pieces = S.pieces.concat([p]); });
  }
  function eraseAt(xy) {
    const hit = pieceAt(xy);
    if (hit === null) return;
    batched(() => { S.pieces = S.pieces.filter(p => p.id !== hit); S.sel.delete(hit); });
  }
  function endBatch() {
    if (drag.any) { S.undo.push(drag.batch); if (S.undo.length > 200) S.undo.shift(); renderSide(); }
  }

  cv.addEventListener('pointermove', ev => {
    if (ev.pointerType === 'touch' && touches.has(ev.pointerId)) touches.set(ev.pointerId, [ev.clientX, ev.clientY]);
    if (drag.mode === 'pinch' && touches.size === 2) {
      const p = pinchState(), s0 = drag.pinch;
      S.cam.k = Math.max(0.08, Math.min(8, s0.k * p.d / s0.d));
      S.cam.x = s0.cx - (p.mx - s0.mx) / S.cam.k;
      S.cam.y = s0.cy - (p.my - s0.my) / S.cam.k;
      applyCamera();
      return;
    }
    const xy = toWorld(ev);
    switch (drag.mode) {
      case 'pan':
        S.cam.x = drag.cx - (ev.clientX - drag.sx) / S.cam.k;
        S.cam.y = drag.cy - (ev.clientY - drag.sy) / S.cam.k;
        applyCamera();
        return;
      case 'place': placeAt(xy); drawGhost(null); return;
      case 'erase': eraseAt(xy); return;
      case 'move': {
        const [a, b] = M.toLattice(xy[0] - drag.start[0], xy[1] - drag.start[1]);
        const d = [Math.round(a), Math.round(b)];
        if (d[0] === drag.d[0] && d[1] === drag.d[1]) return;
        drag.d = d;
        const moved = M.translate(selected(), d[0], d[1]);
        drag.ok = fits(moved, ownerMap(S.sel));
        drawGhost(d[0] || d[1] ? moved : null, drag.ok);
        return;
      }
      case 'marquee': {
        const r = $('#marquee');
        const x = Math.min(drag.start[0], xy[0]), y = Math.min(drag.start[1], xy[1]);
        r.setAttribute('x', x); r.setAttribute('y', y);
        r.setAttribute('width', Math.abs(xy[0] - drag.start[0])); r.setAttribute('height', Math.abs(xy[1] - drag.start[1]));
        r.setAttribute('display', '');
        drag.end = xy;
        return;
      }
    }
    // hover: preview of the panel to place
    if (['hex', 'tri', 'mini'].includes(S.tool) && !S.pick && ev.pointerType !== 'touch') {
      const p = snapped(S.tool, xy[0], xy[1]);
      drawGhost([p], fits([p], S.an ? S.an.owner : ownerMap()));
    }
  });

  function endPointer(ev) {
    if (ev.pointerType === 'touch') {
      touches.delete(ev.pointerId);
      if (drag.mode === 'pinch') { if (!touches.size) drag.mode = null; return; }
    }
    const mode = drag.mode;
    drag.mode = null;
    cv.classList.remove('panning');
    if (mode === 'place' || mode === 'erase') endBatch();
    if (mode === 'move') {
      drawGhost(null);
      if (drag.ok && (drag.d[0] || drag.d[1])) {
        const moved = new Map(M.translate(selected(), drag.d[0], drag.d[1]).map(p => [p.id, p]));
        commit(() => { S.pieces = S.pieces.map(p => moved.get(p.id) || p); });
      } else if (!drag.ok) toast('Panels overlap there', true);
    }
    if (mode === 'marquee') {
      $('#marquee').setAttribute('display', 'none');
      if (drag.end) {
        const x0 = Math.min(drag.start[0], drag.end[0]), x1 = Math.max(drag.start[0], drag.end[0]);
        const y0 = Math.min(drag.start[1], drag.end[1]), y1 = Math.max(drag.start[1], drag.end[1]);
        for (const p of S.pieces) {
          const c = M.centre(p);
          if (c[0] >= x0 && c[0] <= x1 && c[1] >= y0 && c[1] <= y1) S.sel.add(p.id);
        }
        drag.end = null;
        render();
      }
    }
  }
  cv.addEventListener('pointerup', endPointer);
  cv.addEventListener('pointercancel', endPointer);
  cv.addEventListener('pointerleave', () => { if (!drag.mode) drawGhost(null); });
  cv.addEventListener('wheel', ev => {
    ev.preventDefault();
    const [x, y] = toWorld(ev);
    zoomAt(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)), x, y);
  }, {passive: false});

  // ---- keyboard ------------------------------------------------------------------------------
  document.addEventListener('keydown', ev => {
    if (ev.target.closest && ev.target.closest('input, textarea, select, dialog')) return;
    const k = ev.key.toLowerCase(), mod = ev.ctrlKey || ev.metaKey;
    if (mod && k === 'z') { ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
    if (mod && k === 'y') { ev.preventDefault(); redo(); return; }
    if (mod && k === 'a') { ev.preventDefault(); setTool('select'); S.sel = new Set(S.pieces.map(p => p.id)); render(); return; }
    if (mod && k === 'd') { ev.preventDefault(); duplicate(); return; }
    if (mod) return;
    if (k === ' ') { spaceDown = true; cv.classList.add('pan'); ev.preventDefault(); return; }
    const tools = {v: 'select', h: 'hex', t: 'tri', m: 'mini', e: 'erase'};
    if (tools[k]) { setTool(tools[k]); return; }
    if (k === 'r') transformSelection(ps => M.rotate(ps, ev.shiftKey ? 5 : 1), 'rotate');
    else if (k === 'f') transformSelection(ps => M.mirror(ps), 'mirror');
    else if (k === 'delete' || k === 'backspace') deleteSelection();
    else if (k === 'escape') { if (S.pick) setPick(S.pick); else { S.sel.clear(); render(); } }
    else if (k === '0') fit();
    else if (k === 'c') setView(S.view === 'design' ? 'connections' : 'design');
    else if (k === 'd') toggleDims();
    else if (k === '+' || k === '=') zoomAt(1.25);
    else if (k === '-') zoomAt(0.8);
  });
  document.addEventListener('keyup', ev => {
    if (ev.key === ' ') { spaceDown = false; cv.classList.toggle('pan', S.tool === 'select'); }
  });
  function deleteSelection() {
    if (!S.sel.size) return;
    commit(() => { S.pieces = S.pieces.filter(p => !S.sel.has(p.id)); S.sel.clear(); });
  }

  // ---- examples ---------------------------------------------------------------------------
  const rot0 = (p, n) => {           // rotate n × 60° about lattice vertex (0, 0)
    let v = p.v;
    for (let i = 0; i < n; i++) v = v.map(([q, r]) => [-r, q + r]);
    return {id: 0, type: p.type, v};
  };
  /** A panel of `type` touching `target`, not overlapping anything in ps. */
  function attach(ps, target, type) {
    const owner = new Map();
    for (const p of ps) for (const [q, r, s] of M.cells(p)) owner.set(M.ckey(q, r, s), p.id);
    const edges = M.boundary(target).filter(e => ps.every(p => p === target || !M.boundary(p).some(b => b.key === e.key)));
    const [cq, cr] = M.pivot([target]);
    for (const e of edges) {
      for (let q = cq - 4; q <= cq + 4; q++) for (let r = cr - 4; r <= cr + 4; r++) for (const down of [false, true]) {
        const c = M.make(type, q, r, down);
        if (M.boundary(c).some(b => b.key === e.key) && fits([c], owner)) return c;
      }
    }
    return null;
  }
  const EXAMPLES = {
    honeycomb() {
      return [[0, 0], [2, -1], [1, 1], [-1, 2], [-2, 1], [-1, -1], [1, -2]].map(([q, r]) => M.make('hex', q, r));
    },
    star() {
      const h = M.make('hex', 0, 0), t = M.make('tri', -1, 0, true), out = [h];
      for (let i = 0; i < 6; i++) out.push(M.clone(rot0(t, i)));
      return out;
    },
    wave() {
      let c = [0, 0];
      const out = [];
      for (let i = 0; i < 20; i++) { out.push(M.make('hex', c[0], c[1])); c = i % 2 ? [c[0] + 1, c[1] + 1] : [c[0] + 2, c[1] - 1]; }
      for (const [target, type] of [[out[0], 'tri'], [out[19], 'tri'], [out[0], 'mini'], [out[19], 'mini'], [out[9], 'mini'], [out[10], 'mini']]) {
        const p = attach(out, target, type);
        if (p) out.push(p);
      }
      return out;
    },
  };

  // ---- file actions -------------------------------------------------------------------------
  function download(name, data, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([data], {type}));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  function templateSVG() {
    const b = M.bounds(S.pieces), m = 40, plan = S.plan;
    const x0 = b.x0 - m, y0 = b.y0 - m - 20, w = b.w + 2 * m, h = b.h + 2 * m + 30;
    const order = new Map();
    if (plan) { let i = 1; for (const id of [plan.controller && plan.controller.piece].concat(plan.tree.map(t => t.child))) if (id) order.set(id, i++); }
    const o = [];
    o.push('<svg xmlns="http://www.w3.org/2000/svg" width="' + w.toFixed(1) + 'mm" height="' + h.toFixed(1) + 'mm" viewBox="' +
      [x0, y0, w, h].map(n => n.toFixed(2)).join(' ') + '" font-family="sans-serif">');
    o.push('<text x="' + (b.x0).toFixed(1) + '" y="' + (b.y0 - m + 4).toFixed(1) + '" font-size="8">LeafBus layout — ' +
      S.pieces.length + ' panels, ' + (b.w / 10).toFixed(1) + ' × ' + (b.h / 10).toFixed(1) + ' cm. Print at 100 % (no scaling).</text>');
    for (const p of S.pieces) {
      o.push('<polygon points="' + pts(p) + '" fill="none" stroke="#000" stroke-width="0.6"/>');
      const c = M.centre(p);
      if (order.has(p.id)) o.push('<text x="' + c[0].toFixed(1) + '" y="' + (c[1] + 3).toFixed(1) + '" font-size="9" text-anchor="middle">' + order.get(p.id) + '</text>');
    }
    const rect = (e, dash) => '<rect x="-14" y="-3.5" width="28" height="7" rx="2" fill="none" stroke="#000" stroke-width="0.6"' +
      (dash ? ' stroke-dasharray="2 1.5"' : '') + ' transform="translate(' + e.mid[0].toFixed(2) + ' ' + e.mid[1].toFixed(2) + ') rotate(' + edgeAngle(e).toFixed(1) + ')"/>';
    if (plan) {
      for (const t of plan.tree) o.push(rect(t.edge, false));
      for (const x of plan.extras) o.push(rect(x.edge, true));
      const mark = (e, label) => {
        const x = e.mid[0] + e.normal[0] * 16, y = e.mid[1] + e.normal[1] * 16;
        return '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="7" fill="none" stroke="#000" stroke-width="0.6"/>' +
          '<text x="' + x.toFixed(1) + '" y="' + (y + 2.5).toFixed(1) + '" font-size="7" text-anchor="middle">' + label + '</text>';
      };
      if (plan.controller) o.push(mark(plan.controller.edge, 'C'));
      plan.psus.forEach((s, i) => o.push(mark(s.edge, 'P' + (plan.psus.length > 1 ? i + 1 : ''))));
    }
    // start panel: crosshair through its centre to the edges of the design, with distances
    const st = startPanel(), [cx, cy] = st.c, f = n => n.toFixed(1);
    const ln = (x1, y1, x2, y2, dash) => '<line x1="' + f(x1) + '" y1="' + f(y1) + '" x2="' + f(x2) + '" y2="' + f(y2) +
      '" stroke="#000" stroke-width="0.4"' + (dash ? ' stroke-dasharray="3 2"' : '') + '/>';
    const tx = (x, y, s, rot) => '<text x="' + f(x) + '" y="' + f(y) + '" font-size="6" text-anchor="middle"' +
      (rot ? ' transform="rotate(-90 ' + f(x) + ' ' + f(y) + ')"' : '') + '>' + s + '</text>';
    o.push('<rect x="' + f(b.x0) + '" y="' + f(b.y0) + '" width="' + f(b.w) + '" height="' + f(b.h) +
      '" fill="none" stroke="#000" stroke-width="0.3" stroke-dasharray="1 2"/>');
    o.push(ln(b.x0, cy, b.x1, cy, true), ln(cx, b.y0, cx, b.y1, true));
    o.push(ln(cx - 10, cy, cx + 10, cy), ln(cx, cy - 10, cx, cy + 10),
      '<circle cx="' + f(cx) + '" cy="' + f(cy) + '" r="5" fill="none" stroke="#000" stroke-width="0.6"/>');
    o.push(tx(cx, cy + 14, 'START'));
    o.push(tx((b.x0 + cx) / 2, cy - 2, fmtLen(st.left)), tx((cx + b.x1) / 2, cy - 2, fmtLen(st.right)),
      tx(cx - 2, (b.y0 + cy) / 2, fmtLen(st.up), true), tx(cx - 2, (cy + b.y1) / 2, fmtLen(st.down), true));
    o.push(tx((b.x0 + b.x1) / 2, b.y0 - 6, 'width ' + fmtLen(b.w)), tx(b.x1 + 8, (b.y0 + b.y1) / 2, 'height ' + fmtLen(b.h), true));
    o.push('<text x="' + b.x0.toFixed(1) + '" y="' + (b.y1 + m - 4).toFixed(1) + '" font-size="6">Mount the START panel first: ' +
      'the design then reaches ' + fmtLen(st.left) + ' left, ' + fmtLen(st.right) + ' right, ' + fmtLen(st.up) + ' up and ' +
      fmtLen(st.down) + ' down from its centre. Numbers: order along the bus from the controller (C). ' +
      'Solid boxes: linkers the bus needs. Dashed: optional linkers. P: power supply.</text>');
    o.push('</svg>');
    return o.join('\n');
  }

  async function fetchLayout(addr) {
    let base = addr.trim().replace(/\/+$/, '');
    if (!/^https?:\/\//.test(base)) base = 'http://' + base;
    const get = async path => {
      const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 8000);
      try {
        const r = await fetch(base + path, {signal: ctl.signal});
        if (!r.ok) throw new Error(path + ': HTTP ' + r.status);
        return await r.json();
      } finally { clearTimeout(t); }
    };
    try { return await get('/api/layout'); } catch (e) { return await get('/layout'); }
  }

  async function loadFromBoard() {
    let addr = 'leafbus.local';
    try { addr = localStorage.getItem('leafbus.designer.board') || addr; } catch (e) { /* ignore */ }
    const inp = document.createElement('input');
    inp.type = 'text'; inp.value = addr;
    const box = document.createElement('div');
    box.innerHTML = '<p class="small muted" style="margin-top:0">Address of a board running LeafBus (name or IP). The layout comes from the panels themselves; the controller and supply are placed where the board reports them.</p>';
    box.append(inp);
    if (!await ask('Load from a LeafBus board', box, 'Load')) return;
    addr = inp.value.trim();
    try { localStorage.setItem('leafbus.designer.board', addr); } catch (e) { /* ignore */ }
    toast('Asking ' + addr + '…');
    let lay;
    try { lay = await fetchLayout(addr); } catch (e) {
      toast('Could not reach ' + addr + ': ' + (e.name === 'AbortError' ? 'timed out' : e.message), true);
      return;
    }
    if (lay.error) toast('Board reports: ' + lay.error, true);
    const r = M.fromBoard(lay);
    if (!r.pieces.length) { toast('The board reported no panels of a known shape', true); return; }
    commit(() => { S.pieces = r.pieces; S.sel.clear(); S.ctl = r.controller; S.psus = r.psus; });
    setTool('select');
    setView('connections');
    fit();
    toast('Loaded ' + r.pieces.length + ' panels' + (r.skipped ? ' (' + r.skipped + ' of unknown shape left out)' : '') +
      '. Controller and supply are where the board has them — press Auto to see the best spots.');
  }

  async function fileAction(a) {
    $('#filemenu').classList.remove('open');
    if (a === 'new') {
      if (S.pieces.length && !await ask('New layout', 'Remove all panels? (Undo brings them back.)', 'Remove')) return;
      commit(() => { S.pieces = []; S.sel.clear(); S.ctl = null; S.psus = []; });
    } else if (a.startsWith('example-')) {
      const ps = EXAMPLES[a.slice(8)]();
      M.reserveIds(ps);
      commit(() => { S.pieces = ps; S.sel.clear(); S.ctl = null; S.psus = []; });
      setTool('select');
      fit();
    } else if (a === 'open') {
      $('#fileinput').click();
    } else if (a === 'save') {
      download('leafbus-layout.json', JSON.stringify(exportDesign(), null, 1), 'application/json');
    } else if (a === 'share') {
      const url = location.href.split('#')[0] + shareHash();
      history.replaceState(null, '', shareHash());
      try { await navigator.clipboard.writeText(url); toast('Link copied'); }
      catch (e) { toast('Link is in the address bar'); }
    } else if (a === 'svg') {
      if (!S.pieces.length) { toast('Nothing to export yet', true); return; }
      download('leafbus-template.svg', templateSVG(), 'image/svg+xml');
    } else if (a === 'board') {
      loadFromBoard();
    }
  }

  // ---- wiring the page -------------------------------------------------------------------------
  $$('#tools [data-t]').forEach(b => b.onclick = () => setTool(b.dataset.t));
  $$('#tools [data-a]').forEach(b => b.onclick = () => {
    const a = b.dataset.a;
    if (a === 'rotate') transformSelection(ps => M.rotate(ps, 1), 'rotate');
    else if (a === 'mirror') transformSelection(ps => M.mirror(ps), 'mirror');
    else if (a === 'delete') deleteSelection();
    else if (a === 'fit') fit();
  });
  $$('#views button').forEach(b => b.onclick = () => setView(b.dataset.v));
  $$('#prefer button').forEach(b => b.onclick = () => { S.prefer = b.dataset.p; S.ctl = null; changed(); });
  $$('[data-pick]').forEach(b => b.onclick = () => setPick(b.dataset.pick));
  $('#extras').onchange = e => { S.showExtras = e.target.checked; render(); };
  function toggleDims() { S.dims = !S.dims; changed(); }
  $('#dims').onclick = toggleDims;
  $$('#units button').forEach(b => b.onclick = () => { S.units = b.dataset.u; changed(); });
  $('#undo').onclick = undo;
  $('#redo').onclick = redo;
  $('#zin').onclick = () => zoomAt(1.25);
  $('#zout').onclick = () => zoomAt(0.8);
  $('#filebtn').onclick = ev => { ev.stopPropagation(); $('#filemenu').classList.toggle('open'); };
  document.addEventListener('click', ev => { if (!ev.target.closest('#filemenu')) $('#filemenu').classList.remove('open'); });
  $$('#filemenu [data-a]').forEach(b => b.onclick = () => fileAction(b.dataset.a));
  $('#fileinput').onchange = async ev => {
    const f = ev.target.files[0];
    ev.target.value = '';
    if (!f) return;
    try { importDesign(JSON.parse(await f.text())); fit(); toast('Opened ' + f.name); }
    catch (e) { toast('Could not open ' + f.name + ': ' + e.message, true); }
  };
  $('#sidebtn').onclick = () => $('#side').classList.toggle('open');
  // Cancel is a plain button so that Enter submits with OK
  $('#dlgcancel').onclick = () => $('#dlg').close('cancel');
  function syncInputs() {
    for (const k of ['psu', 'hex', 'tri', 'mini']) $('#w-' + k).value = S.power[k];
    $('#extras').checked = S.showExtras;
  }
  for (const k of ['psu', 'hex', 'tri', 'mini']) {
    $('#w-' + k).onchange = e => {
      const v = parseFloat(e.target.value);
      if (v >= 0 && (k !== 'psu' || v > 0)) S.power[k] = v; else e.target.value = S.power[k];
      changed();
    };
  }
  addEventListener('resize', applyCamera);
  addEventListener('hashchange', () => { if (location.hash.startsWith('#d=')) { fromHash(location.hash); fit(); } });

  // ---- start ------------------------------------------------------------------------------
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (e) { saved = null; }
  if (saved && Array.isArray(saved.pieces)) {
    S.pieces = saved.pieces; S.ctl = saved.ctl || null; S.psus = saved.psus || [];
    S.power = Object.assign({}, M.DEFAULT_POWER, saved.power || {});
    S.prefer = saved.prefer || 'shallow';
    S.view = saved.view || 'design';
    if (saved.dims === false) S.dims = false;
    if (saved.units === 'in') S.units = 'in';
    M.reserveIds(S.pieces);
  }
  syncInputs();
  setTool(S.pieces.length ? 'select' : 'hex');
  setView(S.view);
  if (location.hash.startsWith('#d=')) { fromHash(location.hash); requestAnimationFrame(fit); }
  refresh();
  applyCamera();
  requestAnimationFrame(fit);
  // for debugging in the console and for tests
  window.LeafDesigner = {S, refresh, setView, setTool, fileAction, exportDesign, importDesign, shareHash, templateSVG};
})();
