// node --test designer/test
const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('../model.js');
const fixtures = require('./fixtures.json');

const hex = (q, r) => M.make('hex', q, r);
const tri = (q, r, down) => M.make('tri', q, r, down);
const mini = (q, r, down) => M.make('mini', q, r, down);

test('panels cover the right lattice cells and have the right connectors', () => {
  for (const [p, cells, conns] of [[hex(0, 0), 6, 6], [tri(0, 0), 4, 6], [tri(0, 0, true), 4, 6],
                                   [mini(0, 0), 1, 3], [mini(0, 0, true), 1, 3]]) {
    assert.equal(M.cells(p).length, cells, p.type);
    assert.equal(M.boundary(p).length, conns, p.type);
  }
});

test('connector positions match the panel geometry of PROTOCOL.md 7.6', () => {
  // Hexagon: connectors at the apothem 58.02 mm; Triangle: two per side at ±33.5 mm
  const h = hex(0, 0), c = M.centre(h);
  for (const e of M.boundary(h)) assert.ok(Math.abs(Math.hypot(e.mid[0] - c[0], e.mid[1] - c[1]) - 58.02) < 0.01);
  const t = tri(0, 0), ct = M.centre(t);
  const d = M.boundary(t).map(e => Math.hypot(e.mid[0] - ct[0], e.mid[1] - ct[1]));
  for (const x of d) assert.ok(Math.abs(x - Math.hypot(33.5, 38.68)) < 0.01);
});

test('neighbours share lattice edges; overlaps are found', () => {
  const ps = [hex(0, 0), hex(2, -1), tri(1, 0), tri(3, 0)];
  const an = M.analyse(ps);
  assert.equal(an.overlaps.length, 0);
  assert.ok(an.adj.get(ps[0].id).has(ps[1].id), 'hexagons side by side');
  const clash = M.analyse([hex(0, 0), mini(0, 0)]);
  assert.equal(clash.overlaps.length, 2);
  const a = [tri(0, 0), tri(0, 0, true)];          // up and down big triangle side by side
  const an2 = M.analyse(a);
  assert.equal(an2.overlaps.length, 0);
  assert.equal(an2.adj.get(a[0].id).get(a[1].id).length, 2, 'two shared unit edges = two possible linkers');
});

test('rotation and mirroring keep panels on the lattice', () => {
  const ps = [hex(0, 0), tri(1, 0), mini(-2, 1, true)];
  const before = M.analyse(ps);
  for (let k = 1; k <= 6; k++) {
    const r = M.rotate(ps, k), an = M.analyse(r);
    assert.equal(an.overlaps.length, 0);
    assert.equal(an.owner.size, before.owner.size);
    assert.equal(an.free.length, before.free.length);
  }
  assert.deepEqual(M.rotate(ps, 6).map(p => p.v), ps.map(p => p.v));
  const m = M.mirror(M.mirror(ps));
  assert.deepEqual(m.map(p => p.v), ps.map(p => p.v));
});

test('encode / decode round trip', () => {
  const ps = [hex(3, -2), tri(1, 0), tri(4, 4, true), mini(-2, 1), mini(0, 0, true)];
  const back = M.decodeAll(M.encodeAll(ps));
  assert.deepEqual(back.map(p => p.v), ps.map(p => p.v));
  assert.deepEqual(back.map(p => p.type), ps.map(p => p.type));
});

test('a chain is driven from its middle', () => {
  // five hexagons in a row: the middle one gives 2 hops at most
  const ps = [0, 1, 2, 3, 4].map(i => hex(2 * i, -i));
  const plan = M.plan(ps);
  assert.ok(plan.ok);
  assert.equal(plan.controller.piece, ps[2].id);
  assert.equal(plan.stats.max, 2);
  assert.equal(plan.tree.length, 4, 'n - 1 linkers');
  assert.equal(plan.extras.length, 0);
});

test('bottom preference puts the controller on the lowest edge', () => {
  const ps = [0, 1, 2, 3, 4].map(i => hex(-i, 2 * i));   // a vertical column
  const plan = M.plan(ps, {prefer: 'bottom'});
  assert.equal(plan.controller.piece, ps[0].id);
  assert.equal(plan.stats.max, 4);
});

test('rings get extra linkers; the tree still spans every panel', () => {
  // six mini triangles around a vertex: a closed ring
  const c = M.make('hex', 0, 0);
  const ring = M.cells(c).map(([q, r, s]) => M.make('mini', q, r, s === 1));
  const plan = M.plan(ring);
  assert.equal(plan.tree.length, 5);
  assert.equal(plan.extras.length, 1);
  assert.equal(plan.stats.max, 3);
});

test('enough power supplies, placed to keep hops short', () => {
  const ps = [];
  for (let i = 0; i < 6; i++) for (let j = 0; j < 4; j++) ps.push(hex(2 * i - j, j * 2 - i));
  assert.equal(M.analyse(ps).overlaps.length, 0);
  const plan = M.plan(ps);              // 24 hexagons, 48 W
  assert.equal(plan.power.needed, 2);
  assert.equal(plan.psus.length, 2);
  assert.ok(!plan.power.overloaded);
  assert.ok(plan.psus.every(p => p.edge.key !== plan.controller.edge.key));
  const single = M.plan(ps, {power: {psu: 100}});
  assert.equal(single.psus.length, 1);
  assert.ok(plan.power.maxHops < single.power.maxHops);
});

test('islands and forced positions', () => {
  const ps = [hex(0, 0), hex(2, -1), hex(10, 10)];
  const plan = M.plan(ps);
  assert.equal(plan.problems[0].kind, 'island');
  assert.deepEqual(plan.unreached, [ps[2].id]);
  const free = M.analyse(ps).free.filter(e => e.piece === ps[0].id);
  const forced = M.plan(ps, {controller: free[0].key, psus: [free[1].key]});
  assert.equal(forced.controller.piece, ps[0].id);
  assert.ok(forced.controller.forced);
  assert.equal(forced.psus[0].edge.key, free[1].key);
});

for (const [name, fx] of Object.entries(fixtures)) {
  test('import ' + name + ' from a board layout', () => {
    const r = M.fromBoard(fx);
    assert.equal(r.pieces.length, fx.panels);
    assert.equal(r.offGrid, 0, 'every corner lands on the lattice');
    const an = M.analyse(r.pieces);
    assert.equal(an.overlaps.length, 0);
    assert.equal(an.components.length, 1);
    // same neighbours as firmware/geometry.py
    const idOf = new Map([...r.chunkOf].map(([id, ch]) => [ch, id]));
    for (let ch = 0; ch < fx.panels; ch++) {
      const mine = [...an.adj.get(idOf.get(ch)).keys()].map(id => r.chunkOf.get(id)).sort((a, b) => a - b);
      assert.deepEqual(mine, fx.neighbours[ch], 'neighbours of chunk ' + ch);
    }
    // controller connector and supply found on free edges of the right panels
    assert.ok(r.controller, 'controller edge');
    assert.equal(r.chunkOf.get(r.controller.piece), fx.panels - 1);
    assert.equal(r.psus.length, 1);
    assert.equal(r.chunkOf.get(r.psus[0].piece), fx.psu.chunk);
    // the plan keeps the board's controller and supply where they are
    const plan = M.plan(r.pieces, {controller: r.controller, psus: r.psus});
    assert.equal(plan.controller.edge.key, r.controller.key);
    assert.ok(plan.controller.forced);
    assert.equal(plan.psus[0].edge.key, r.psus[0].key);
    assert.equal(plan.unreached.length, 0);
  });
}
