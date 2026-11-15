/* LeafBus designer: the layout model and the connection optimiser.
 *
 * Pure functions, no DOM: runs in the browser (window.LeafModel) and in
 * node (require('./model.js')) for the tests in designer/test/.
 *
 * Geometry (PROTOCOL.md §7.6): Hexagons, Triangles and Mini Triangles tile
 * one triangular lattice. A Hexagon covers 6 lattice cells, a Triangle 4,
 * a Mini Triangle 1. Every connector sits at the midpoint of a lattice
 * edge (a Triangle has two per side), so two panels can be linked exactly
 * where they share a lattice edge.
 *
 * Units: the lattice side is L = 67 layout units, the scale of the
 * Nanoleaf API's positionData and of the board's /api/layout, so a board's
 * layout imports without conversion. On the wall that side is 115 mm (a
 * Mini Triangle measures 11.5 x 10 cm, a Hexagon or Triangle 23 x 20 cm):
 * MM_PER_UNIT converts any length here to millimetres.
 *
 * Coordinates: lattice vertex (q, r) is at q·e1 + r·e2 with e1 = (L, 0) and
 * e2 = (L/2, -H), in layout units with y pointing DOWN (screen convention);
 * H = L·√3/2. Cell (q, r, 0) is the upward triangle (q,r)(q+1,r)(q,r+1),
 * cell (q, r, 1) the downward one (q+1,r)(q+1,r+1)(q,r+1).
 *
 * A panel is {id, type: 'hex' | 'tri' | 'mini', v: [[q, r], ...]}: its
 * corner vertices on the lattice. Moving, rotating and mirroring are
 * transforms of those vertices.
 */
(function (root) {
  'use strict';

  const L = 67;
  const MM_PER_UNIT = 115 / 67;   // measured panels: 115 mm lattice side
  const H = L * Math.sqrt(3) / 2;
  const TYPES = {
    hex:  {label: 'Hexagon',       shape: 7, side: 67,  connectors: 6, cells: 6},
    tri:  {label: 'Triangle',      shape: 8, side: 134, connectors: 6, cells: 4},
    mini: {label: 'Mini Triangle', shape: 9, side: 67,  connectors: 3, cells: 1},
  };
  // Nanoleaf spec sheets: 42 W supply; 21 Hexagons, 28 Triangles or 77 Mini Triangles
  const DEFAULT_POWER = {psu: 42, hex: 2, tri: 1.5, mini: 0.54};

  // ---- lattice -------------------------------------------------------------
  const P = (q, r) => [q * L + r * L / 2, -r * H];
  const toLattice = (x, y) => { const b = -y / H; return [x / L - b / 2, b]; };
  const vkey = v => v[0] + ',' + v[1];
  const ekey = (a, b) => {
    const ka = vkey(a), kb = vkey(b);
    return ka < kb ? ka + '|' + kb : kb + '|' + ka;
  };
  const ckey = (q, r, s) => q + ',' + r + ',' + s;

  function cellAt(x, y) {
    const [a, b] = toLattice(x, y);
    const q = Math.floor(a), r = Math.floor(b);
    return [q, r, (a - q) + (b - r) < 1 ? 0 : 1];
  }

  function cellVerts(q, r, s) {
    return s === 0 ? [[q, r], [q + 1, r], [q, r + 1]]
                   : [[q + 1, r], [q + 1, r + 1], [q, r + 1]];
  }

  // ---- panels ----------------------------------------------------------------
  const TEMPLATES = {
    'mini-up':   [[0, 0], [1, 0], [0, 1]],
    'mini-down': [[1, 0], [1, 1], [0, 1]],
    'tri-up':    [[0, 0], [2, 0], [0, 2]],
    'tri-down':  [[2, 0], [2, 2], [0, 2]],
    'hex':       [[1, 0], [0, 1], [-1, 1], [-1, 0], [0, -1], [1, -1]],
  };

  let nextId = 1;
  function make(type, q, r, down) {
    const t = TEMPLATES[type === 'hex' ? 'hex' : type + (down ? '-down' : '-up')];
    return {id: nextId++, type, v: t.map(([a, b]) => [a + q, b + r])};
  }

  /** Keep make() from reusing ids of panels loaded from elsewhere. */
  function reserveIds(ps) {
    for (const p of ps) if (p.id >= nextId) nextId = p.id + 1;
  }

  function clone(p, keepId) {
    return {id: keepId ? p.id : nextId++, type: p.type, v: p.v.map(x => x.slice())};
  }

  /** Point (lattice coordinates) strictly inside a convex polygon. */
  function inside(poly, a, b) {
    let sign = 0;
    for (let i = 0; i < poly.length; i++) {
      const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % poly.length];
      const c = (x2 - x1) * (b - y1) - (y2 - y1) * (a - x1);
      if (Math.abs(c) < 1e-9) return false;
      const s = c > 0 ? 1 : -1;
      if (sign && s !== sign) return false;
      sign = s;
    }
    return true;
  }

  /** Lattice cells covered by a panel: those whose centroid lies inside. */
  function cells(p) {
    const qs = p.v.map(v => v[0]), rs = p.v.map(v => v[1]);
    const out = [];
    for (let q = Math.min(...qs) - 1; q <= Math.max(...qs); q++) {
      for (let r = Math.min(...rs) - 1; r <= Math.max(...rs); r++) {
        if (inside(p.v, q + 1 / 3, r + 1 / 3)) out.push([q, r, 0]);
        if (inside(p.v, q + 2 / 3, r + 2 / 3)) out.push([q, r, 1]);
      }
    }
    return out;
  }

  function centre(p) {
    const n = p.v.length;
    let x = 0, y = 0;
    for (const v of p.v) { const w = P(v[0], v[1]); x += w[0]; y += w[1]; }
    return [x / n, y / n];
  }

  /** Outer lattice edges of a panel = its connectors. */
  function boundary(p) {
    const seen = new Map();
    for (const [q, r, s] of cells(p)) {
      const cv = cellVerts(q, r, s);
      for (let i = 0; i < 3; i++) {
        const a = cv[i], b = cv[(i + 1) % 3], k = ekey(a, b);
        const e = seen.get(k);
        if (e) e.n++; else seen.set(k, {key: k, a, b, n: 1});
      }
    }
    const c = centre(p);
    const out = [];
    for (const e of seen.values()) {
      if (e.n !== 1) continue;
      const pa = P(e.a[0], e.a[1]), pb = P(e.b[0], e.b[1]);
      const mid = [(pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2];
      const dx = mid[0] - c[0], dy = mid[1] - c[1], d = Math.hypot(dx, dy);
      out.push({key: e.key, a: e.a, b: e.b, mid, normal: [dx / d, dy / d], piece: p.id});
    }
    return out;
  }

  // ---- transforms (all keep panels on the lattice) ------------------------------
  const rot60 = ([q, r], [cq, cr]) => { const a = q - cq, b = r - cr; return [cq - b, cr + a + b]; };
  const mirrorX = ([q, r], [cq, cr]) => { const a = q - cq, b = r - cr; return [cq + a + b, cr - b]; };
  function transform(ps, f) { return ps.map(p => ({id: p.id, type: p.type, v: p.v.map(f)})); }
  function translate(ps, dq, dr) { return transform(ps, ([q, r]) => [q + dq, r + dr]); }
  /** Lattice vertex nearest to the centre of some panels (pivot for rotations). */
  function pivot(ps) {
    let x = 0, y = 0, n = 0;
    for (const p of ps) { const c = centre(p); x += c[0]; y += c[1]; n++; }
    const [a, b] = toLattice(x / n, y / n);
    return [Math.round(a), Math.round(b)];
  }
  function rotate(ps, times) {
    const c = pivot(ps);
    let out = ps;
    for (let i = 0; i < ((times % 6) + 6) % 6; i++) out = transform(out, v => rot60(v, c));
    return out;
  }
  function mirror(ps) { const c = pivot(ps); return transform(ps, v => mirrorX(v, c)); }

  // ---- compact form (files, share links) --------------------------------------------
  /** 'h q r' (centre), 't q r u|d' or 'm q r u|d' (anchor = min corner). */
  function encode(p) {
    if (p.type === 'hex') {
      const q = p.v.reduce((s, v) => s + v[0], 0) / 6, r = p.v.reduce((s, v) => s + v[1], 0) / 6;
      return 'h' + q + ',' + r;
    }
    const q = Math.min(...p.v.map(v => v[0])), r = Math.min(...p.v.map(v => v[1]));
    const up = p.v.some(v => v[0] === q && v[1] === r);
    return (p.type === 'tri' ? 't' : 'm') + q + ',' + r + (up ? 'u' : 'd');
  }
  function decode(s) {
    const m = /^([htm])(-?\d+),(-?\d+)([ud]?)$/.exec(s.trim());
    if (!m) throw new Error('bad panel ' + JSON.stringify(s));
    const type = {h: 'hex', t: 'tri', m: 'mini'}[m[1]];
    return make(type, +m[2], +m[3], m[4] === 'd');
  }
  const encodeAll = ps => ps.map(encode).join(';');
  const decodeAll = s => s ? s.split(';').filter(Boolean).map(decode) : [];

  // ---- analysis -------------------------------------------------------------------
  /**
   * {owner: cell -> id, overlaps: [id], byId, edges: edgeKey -> [boundary edge],
   *  adj: id -> Map(neighbour id -> [edgeKey]), free: [boundary edge on the
   *  outside], components: [[id]]}
   */
  function analyse(ps, occupied) {
    occupied = occupied || new Set();       // shared edges taken by a controller / supply
    const owner = new Map(), overlaps = new Set(), byId = new Map();
    const edges = new Map(), adj = new Map();
    for (const p of ps) {
      byId.set(p.id, p);
      adj.set(p.id, new Map());
      for (const [q, r, s] of cells(p)) {
        const k = ckey(q, r, s);
        if (owner.has(k)) { overlaps.add(p.id); overlaps.add(owner.get(k)); } else owner.set(k, p.id);
      }
      for (const e of boundary(p)) {
        if (!edges.has(e.key)) edges.set(e.key, []);
        edges.get(e.key).push(e);
      }
    }
    const free = [];
    for (const [k, es] of edges) {
      if (es.length === 1) { free.push(es[0]); continue; }
      const [x, y] = es;
      if (es.length !== 2 || x.piece === y.piece || occupied.has(k)) continue;
      for (const [a, b] of [[x.piece, y.piece], [y.piece, x.piece]]) {
        const m = adj.get(a);
        if (!m.has(b)) m.set(b, []);
        m.get(b).push(k);
      }
    }
    // components
    const comp = new Map(), components = [];
    for (const p of ps) {
      if (comp.has(p.id)) continue;
      const list = [p.id];
      comp.set(p.id, components.length);
      for (let i = 0; i < list.length; i++) {
        for (const n of adj.get(list[i]).keys()) {
          if (!comp.has(n)) { comp.set(n, components.length); list.push(n); }
        }
      }
      components.push(list);
    }
    components.sort((a, b) => b.length - a.length);
    return {owner, overlaps: [...overlaps], byId, edges, adj, free, components};
  }

  function bfs(adj, sources) {
    const depth = new Map(), parent = new Map(), order = [];
    for (const s of sources) { depth.set(s, 0); order.push(s); }
    for (let i = 0; i < order.length; i++) {
      const u = order[i];
      // neighbours in a stable order so plans do not flicker between edits
      for (const n of [...adj.get(u).keys()].sort((a, b) => a - b)) {
        if (!depth.has(n)) { depth.set(n, depth.get(u) + 1); parent.set(n, u); order.push(n); }
      }
    }
    return {depth, parent, order};
  }

  function stats(depth, ids) {
    let max = 0, sum = 0, n = 0;
    for (const id of ids) {
      const d = depth.get(id);
      if (d === undefined) continue;
      max = Math.max(max, d); sum += d; n++;
    }
    return {max, avg: n ? sum / n : 0, reached: n};
  }

  function power(ps, cfg) {
    let w = 0;
    for (const p of ps) w += cfg[p.type] || 0;
    return w;
  }

  /** The edge that takes the linker between two panels: the one nearest the
   *  line between their centres. */
  function linkerEdge(an, a, b) {
    const keys = an.adj.get(a).get(b);
    if (keys.length === 1) return keys[0];
    const ca = centre(an.byId.get(a)), cb = centre(an.byId.get(b));
    const m = [(ca[0] + cb[0]) / 2, (ca[1] + cb[1]) / 2];
    let best = keys[0], bd = Infinity;
    for (const k of keys) {
      const e = an.edges.get(k)[0], d = Math.hypot(e.mid[0] - m[0], e.mid[1] - m[1]);
      if (d < bd - 1e-6) { bd = d; best = k; }
    }
    return best;
  }

  /**
   * The connection plan.
   *
   * opts: {controller: edge key or {key, piece} to force, psus: [same],
   *        prefer: 'shallow' | 'bottom', power: {psu, hex, tri, mini}}
   *
   * Returns {ok, problems[], controller, alternatives[], depth (Map), tree[],
   *          extras[], psus[], power{}}.
   */
  function plan(ps, opts) {
    opts = opts || {};
    const cfg = Object.assign({}, DEFAULT_POWER, opts.power || {});
    // a forced controller / supply may sit on a connector between two panels
    // (an imported board): that spot then cannot take a linker
    const want = x => x && (typeof x === 'string' ? {key: x} : x);
    const ctlWant = want(opts.controller);
    const psuWant = (opts.psus || []).map(want).filter(Boolean);
    const occupied = new Set([ctlWant].concat(psuWant).filter(Boolean).map(w => w.key));
    const an = analyse(ps, occupied);
    const edgeFor = w => {
      const es = an.edges.get(w.key) || [];
      return es.find(e => w.piece === undefined || e.piece === w.piece) || null;
    };
    const out = {ok: true, problems: [], analysis: an, controller: null, alternatives: [],
                 depth: new Map(), tree: [], extras: [], psus: [], power: null};
    if (!ps.length) { out.ok = false; return out; }
    if (an.overlaps.length) {
      out.ok = false;
      out.problems.push({kind: 'overlap', ids: an.overlaps,
                         text: an.overlaps.length + ' panels overlap'});
    }
    if (an.components.length > 1) {
      out.problems.push({kind: 'island', ids: [].concat(...an.components.slice(1)),
                         text: (an.components.length - 1) + ' group(s) of panels do not touch the rest and cannot be linked'});
    }
    const main = an.components[0];
    const inMain = new Set(main);
    const ids = ps.map(p => p.id);
    const freeOf = new Map();
    for (const e of an.free) {
      if (!inMain.has(e.piece)) continue;
      if (!freeOf.has(e.piece)) freeOf.set(e.piece, []);
      freeOf.get(e.piece).push(e);
    }
    // lowest edge first (outlets are usually below), then left to right
    for (const list of freeOf.values()) list.sort((a, b) => b.mid[1] - a.mid[1] || a.mid[0] - b.mid[0]);

    // --- controller: try every panel that has a free connector
    const cands = [];
    for (const [id, list] of freeOf) {
      const st = stats(bfs(an.adj, [id]).depth, main);
      cands.push({piece: id, edge: list[0], max: st.max, avg: st.avg, low: list[0].mid[1]});
    }
    const prefer = opts.prefer || 'shallow';
    cands.sort(prefer === 'bottom'
      ? (a, b) => (b.low - a.low) || (a.max - b.max) || (a.avg - b.avg)
      : (a, b) => (a.max - b.max) || (a.avg - b.avg) || (b.low - a.low) || (a.piece - b.piece));
    let ctl = cands[0];
    if (ctlWant) {
      const e = edgeFor(ctlWant);
      if (e && inMain.has(e.piece)) {
        const st = stats(bfs(an.adj, [e.piece]).depth, main);
        ctl = {piece: e.piece, edge: e, max: st.max, avg: st.avg, low: e.mid[1], forced: true};
      }
    }
    if (!ctl) { out.ok = false; out.problems.push({kind: 'nofree', text: 'no free connector for the controller'}); return out; }
    out.controller = ctl;
    out.alternatives = cands.slice(0, 5);

    // --- data tree: breadth first from the controller panel
    const t = bfs(an.adj, [ctl.piece]);
    out.depth = t.depth;
    const used = new Set();
    for (const id of t.order) {
      if (!t.parent.has(id)) continue;
      const par = t.parent.get(id), k = linkerEdge(an, par, id);
      used.add(k);
      out.tree.push({parent: par, child: id, edge: an.edges.get(k)[0], depth: t.depth.get(id)});
    }
    for (const [k, es] of an.edges) {
      if (es.length === 2 && es[0].piece !== es[1].piece && !used.has(k) &&
          inMain.has(es[0].piece)) {
        out.extras.push({a: es[0].piece, b: es[1].piece, edge: es[0]});
      }
    }

    // --- power: enough supplies, placed so the farthest panel is as close as
    // possible to its nearest supply; each panel draws from its nearest one
    const total = power(main.map(id => an.byId.get(id)), cfg);
    const need = Math.max(1, Math.ceil(total / cfg.psu - 1e-9));
    const spots = [];
    for (const [id, list] of freeOf) {
      for (const e of list) if (e.key !== ctl.edge.key) { spots.push({piece: id, edge: e}); break; }
    }
    const dist = new Map();
    const distFrom = id => { if (!dist.has(id)) dist.set(id, bfs(an.adj, [id]).depth); return dist.get(id); };
    const evaluate = chosen => {
      const d = bfs(an.adj, chosen.map(c => c.piece));
      const load = chosen.map(() => 0), members = chosen.map(() => []);
      for (const id of main) {
        let best = 0, bd = Infinity;
        chosen.forEach((c, i) => { const x = distFrom(c.piece).get(id); if (x < bd) { bd = x; best = i; } });
        load[best] += cfg[an.byId.get(id).type] || 0;
        members[best].push(id);
      }
      const st = stats(d.depth, main);
      return {max: st.max, avg: st.avg, load, members, worst: Math.max(...load)};
    };
    const better = (a, b) => a.max !== b.max ? a.max < b.max
      : (a.worst > cfg.psu) !== (b.worst > cfg.psu) ? a.worst <= cfg.psu
      : a.worst !== b.worst ? a.worst < b.worst - 1e-9 : a.avg < b.avg - 1e-9;

    let chosen = [];
    for (const w of psuWant) {
      const e = edgeFor(w);
      if (e && inMain.has(e.piece) && e.key !== ctl.edge.key) chosen.push({piece: e.piece, edge: e, forced: true});
    }
    if (!chosen.length && spots.length) {
      const k = Math.min(need, spots.length);
      // greedy: centre first, then the spot farthest from the chosen ones
      let first = spots[0], fe = null;
      for (const s of spots) { const e = evaluate([s]); if (!fe || better(e, fe)) { fe = e; first = s; } }
      chosen = [first];
      while (chosen.length < k) {
        const d = bfs(an.adj, chosen.map(c => c.piece)).depth;
        let far = null;
        for (const s of spots) {
          if (chosen.some(c => c.piece === s.piece)) continue;
          if (!far || d.get(s.piece) > d.get(far.piece)) far = s;
        }
        if (!far) break;
        chosen.push(far);
      }
      // local search: swap one supply at a time while it improves
      let cur = evaluate(chosen), improved = true, rounds = 0;
      while (improved && rounds++ < 20) {
        improved = false;
        for (let i = 0; i < chosen.length; i++) {
          for (const s of spots) {
            if (chosen.some(c => c.piece === s.piece)) continue;
            const trial = chosen.slice(); trial[i] = s;
            const e = evaluate(trial);
            if (better(e, cur)) { chosen = trial; cur = e; improved = true; }
          }
        }
      }
    }
    const ev = chosen.length ? evaluate(chosen) : {max: 0, avg: 0, load: [], members: []};
    out.psus = chosen.map((c, i) => ({piece: c.piece, edge: c.edge, forced: !!c.forced,
                                      load: ev.load[i], panels: ev.members[i]}));
    out.power = {total, perPsu: cfg.psu, needed: need, maxHops: ev.max, avgHops: ev.avg,
                 overloaded: out.psus.some(p => p.load > cfg.psu + 1e-9)};
    if (out.psus.length < need) {
      out.problems.push({kind: 'power', text: 'needs ' + need + ' supplies but only ' + out.psus.length + ' free connectors are left'});
    } else if (out.power.overloaded) {
      out.problems.push({kind: 'power', text: 'a supply carries more than ' + cfg.psu + ' W: add one or move them'});
    }
    out.stats = stats(t.depth, main);
    out.unreached = ids.filter(id => !t.depth.has(id));
    return out;
  }

  // ---- size ----------------------------------------------------------------------------
  function bounds(ps) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of ps) for (const v of p.v) {
      const [x, y] = P(v[0], v[1]);
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    return ps.length ? {x0, y0, x1, y1, w: x1 - x0, h: y1 - y0} : null;
  }

  // ---- import from a board ------------------------------------------------------------------
  const SHAPE_TYPE = {7: 'hex', 8: 'tri', 9: 'mini'};

  /** Corners of a placed panel from positionData (layout units, y up). */
  function polygonMM(p) {
    const s = p.side || (p.shape === 8 ? 134 : 67);
    const rad = p.shape === 7 ? s : s / Math.sqrt(3);
    const angs = p.shape === 7 ? [0, 60, 120, 180, 240, 300] : [-30, 90, 210];
    return angs.map(a => {
      const t = (a + (p.o || 0)) * Math.PI / 180;
      return [p.x + rad * Math.cos(t), p.y + rad * Math.sin(t)];
    });
  }

  /**
   * Panels from a board's /api/layout (or /layout) reply. Returns
   * {pieces, chunkOf: id -> chunk, controller: {key, piece} | null,
   *  psus: [{key, piece}], skipped: n, offGrid: n}; controller and psus go
   * straight into plan() options.
   */
  function fromBoard(layout) {
    const pos = (layout && layout.positions) || [];
    const usable = pos.filter(p => SHAPE_TYPE[p.shape]);
    if (!usable.length) return {pieces: [], controller: null, psus: [], skipped: pos.length, offGrid: 0};
    // board: y up, origin at the master panel; designer: y down, lattice vertex at 0
    const first = polygonMM(usable[0])[0];
    const toQR = ([x, y]) => toLattice(x - first[0], -(y - first[1]));
    let offGrid = 0;
    const snap = xy => {
      const [a, b] = toQR(xy), q = Math.round(a), r = Math.round(b);
      if (Math.abs(a - q) > 0.05 || Math.abs(b - r) > 0.05) offGrid++;
      return [q, r];
    };
    const pieces = [], chunkOf = new Map();
    for (const p of usable) {
      const piece = {id: nextId++, type: SHAPE_TYPE[p.shape], v: polygonMM(p).map(snap)};
      chunkOf.set(piece.id, p.chunk);
      pieces.push(piece);
    }
    const idOf = new Map([...chunkOf].map(([id, ch]) => [ch, id]));
    // the connector of a given panel nearest to a board position
    const onPanel = (chunk, xy) => {
      const piece = pieces.find(p => p.id === idOf.get(chunk));
      if (!piece) return null;
      const [a, b] = toQR(xy), pt = P(a, b);
      let best = null, bd = Infinity;
      for (const e of boundary(piece)) {
        const d = Math.hypot(e.mid[0] - pt[0], e.mid[1] - pt[1]);
        if (d < bd) { bd = d; best = e; }
      }
      return best && bd < 20 ? {key: best.key, piece: piece.id} : null;
    };
    const master = pos.find(p => p.chunk === pos.length - 1);
    const controller = master && master.link ? onPanel(master.chunk, master.link) : null;
    const psus = [];
    if (layout.psu && layout.psu.x !== undefined) {
      const w = onPanel(layout.psu.chunk, [layout.psu.x, layout.psu.y]);
      if (w) psus.push(w);
    }
    return {pieces, chunkOf, controller, psus, skipped: pos.length - usable.length, offGrid};
  }

  const api = {L, H, MM_PER_UNIT, TYPES, DEFAULT_POWER, P, toLattice, cellAt, cellVerts, ckey, ekey, vkey,
               make, clone, reserveIds, cells, centre, boundary, translate, rotate, mirror, pivot,
               encode, decode, encodeAll, decodeAll, analyse, bfs, plan, bounds, power,
               polygonMM, fromBoard};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.LeafModel = api;
})(this);
