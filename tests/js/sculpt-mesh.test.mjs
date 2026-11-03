// The sculpt editor's density operations (static/sculpt-mesh.js), in Node on synthetic meshes.
// Run: node --import ./tests/js/vendor-hooks.mjs --test tests/js/*.test.mjs   (tests/test_js.py runs it in pytest)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';
import * as M from '../../src/motiongrammetry/web/static/sculpt-mesh.js';

// A closed, slightly bumpy sphere (~19k faces) with uneven edges like a scan's (each vertex moved
// by up to ±30% of an edge): a perfectly even mesh has no edge just over a threshold, so ×1.5 would
// split nothing and ÷2 would merge in lockstep.
function sphere(detail = 30, bump = 0.01, jitter = 0.3) {
  const g = mergeVertices(new THREE.IcosahedronGeometry(1, detail).deleteAttribute('normal').deleteAttribute('uv'));
  const p = g.attributes.position.array, e = 1.26 / (detail + 1);
  let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5);
  for (let i = 0; i < p.length; i += 3) {
    p[i] += 2 * jitter * e * rnd(); p[i + 1] += 2 * jitter * e * rnd(); p[i + 2] += 2 * jitter * e * rnd();
    const s = (1 + bump * rnd()) / Math.hypot(p[i], p[i + 1], p[i + 2]); p[i] *= s; p[i + 1] *= s; p[i + 2] *= s;
  }
  return scan(new Float32Array(p), new Uint32Array(g.index.array));
}
// A mesh as the editor loads a scan: its density reference is (a copy of) the mesh itself.
function scan(pos, idx) {
  return M.buildSculptState(pos, new Uint8Array(pos.length).fill(200), idx, null, new M.ScanDensity(pos.slice(), idx.slice()));
}
// Two parallel plates 2 edge lengths apart, as the two sides of a thin part.
function plates(n = 60) {
  const pos = [], idx = [];
  for (const z of [0, 2 / n]) {
    const o = pos.length / 3;
    for (let j = 0; j <= n; j++) for (let i = 0; i <= n; i++) pos.push(i / n, j / n, z);
    for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
      const a = o + j * (n + 1) + i, b = a + 1, c = a + n + 1, d = c + 1;
      idx.push(a, b, d, a, d, c);
    }
  }
  return scan(new Float32Array(pos), new Uint32Array(idx));
}
const vec = (S, v) => new THREE.Vector3(S.pos[v * 3], S.pos[v * 3 + 1], S.pos[v * 3 + 2]);
const near = (S, c, r) => { let n = 0; for (let v = 0; v < S.nv; v++) if (vec(S, v).distanceToSquared(c) <= r * r) n++; return n; };
function area(S, t) {
  const [a, b, c] = [0, 1, 2].map((k) => vec(S, S.idx[t * 3 + k]));
  return b.sub(a).cross(c.sub(a)).length() / 2;
}
// vertices per area within r of c, against a mesh of equilateral triangles with edges e0
function density(S, c, r, e0) {
  let A = 0;
  for (let t = 0; t < S.nf; t++) {
    const m = vec(S, S.idx[t * 3]).add(vec(S, S.idx[t * 3 + 1])).add(vec(S, S.idx[t * 3 + 2])).divideScalar(3);
    if (m.distanceToSquared(c) <= r * r) A += area(S, t);
  }
  return near(S, c, r) / A / (2 / (Math.sqrt(3) * e0 * e0));
}
function refineAt(S, c, r, k, budget) {  // as a dab: refined out to twice the brush radius
  const next = M.refine(S, c, 2 * r, k, budget);
  return next ? M.grownState(S, next) : S;
}
// every edge on exactly two faces and every vertex's faces one fan: closed, no crack, no bow-tie
function assertClosedManifold(S) {
  const E = new Map(), link = new Map();
  for (let t = 0; t < S.nf; t++) {
    const f = [0, 1, 2].map((k) => S.idx[t * 3 + k]);
    assert.ok(f[0] !== f[1] && f[1] !== f[2] && f[0] !== f[2], `triangle ${t} is degenerate`);
    for (let k = 0; k < 3; k++) {
      const a = f[k], b = f[(k + 1) % 3], key = Math.min(a, b) * 4e6 + Math.max(a, b);
      E.set(key, (E.get(key) || 0) + 1);
      if (!link.has(a)) link.set(a, []);
      link.get(a).push([b, f[(k + 2) % 3]]);
    }
  }
  for (const [key, n] of E) assert.equal(n, 2, `edge ${key} has ${n} faces`);
  for (const [v, L] of link) {  // the link of v is one cycle: walk it
    const nextOf = new Map(L);
    let x = L[0][0], steps = 0;
    do { x = nextOf.get(x); steps++; } while (x !== L[0][0] && x !== undefined && steps <= L.length);
    assert.equal(steps, L.length, `vertex ${v} is a bow-tie`);
  }
}

const base = sphere(), e0 = M.medianEdge(base.pos, base.idx);
const spots = [0, 1500, 4000, 7000].map((v) => vec(base, v));

test('×k gives k times the scan density in the brushed area (±25%)', () => {
  for (const k of [1.5, 2, 4, 8]) {
    for (const c of spots) {
      const r = 8 * e0, S = refineAt(base, c, r, k);
      const got = density(S, c, r, e0) / density(base, c, r, e0);
      assert.ok(got > 0.75 * k && got < 1.25 * k, `×${k}: got ×${got.toFixed(2)}`);
    }
  }
});

test('the same factor gives the same density at any brush size (zoom)', () => {
  const c = spots[1], small = refineAt(base, c, 4 * e0, 4), large = refineAt(base, c, 16 * e0, 4);
  const r = 2 * e0, a = density(small, c, r, e0), b = density(large, c, r, e0);
  assert.ok(Math.abs(a / b - 1) < 0.2, `${a.toFixed(2)} vs ${b.toFixed(2)}`);
});

test('brushing the same area again at the same factor adds (almost) nothing', () => {
  const c = spots[2], r = 8 * e0, once = refineAt(base, c, r, 4), twice = refineAt(once, c, r, 4);
  assert.ok(twice.nv - once.nv <= 0.02 * (once.nv - base.nv), `${once.nv - base.nv} then ${twice.nv - once.nv} more`);
});

test('a stroke stops at its budget and says so', () => {
  const next = M.refine(base, spots[0], 30 * e0, 8, 500);
  assert.ok(next.added <= 500 && next.capped);
  const S = M.grownState(base, next);
  assertClosedManifold(S);  // stopping part-way leaves no crack either
});

test('refining and decimating keep the mesh closed and manifold (nothing for the save to cut)', () => {
  let S = base;
  for (const [i, c] of spots.entries()) S = refineAt(S, c, (6 + 4 * i) * e0, [2, 4, 8, 3][i]);
  assertClosedManifold(S);
  for (const c of spots) {
    const out = M.decimate(S, [[c.x, c.y, c.z, 10 * e0]], 2);
    if (out) S = M.buildSculptState(out.pos, out.col8, out.idx, out.e0v, base.density);
  }
  assertClosedManifold(S);
});

// On real scans MERGE gives ÷2 and ÷4 within ±15% (orbit, plant, her_face, shoe: see the PR of #13);
// this even synthetic sphere collapses more in lockstep, so the band here is wider. It still catches
// the old behaviour (÷4.5 for "÷2").
test('÷k gives about 1/k of the scan density (±40% on this synthetic mesh)', () => {
  for (const k of [2, 4]) {
    for (const c of spots.slice(0, 2)) {
      const out = M.decimate(base, [[c.x, c.y, c.z, 12 * e0]], k);
      const S = M.buildSculptState(out.pos, out.col8, out.idx, out.e0v, base.density), r = 8 * e0;
      const got = density(base, c, r, e0) / density(S, c, r, e0);
      assert.ok(got > 0.6 * k && got < 1.4 * k, `÷${k}: got ÷${got.toFixed(2)}`);
    }
  }
});

test('after refining, the extra BVHs find what a full search finds, and picking hits new triangles', () => {
  let S = base;
  for (const [i, c] of spots.entries()) S = refineAt(S, c, (5 + 3 * i) * e0, 4);
  assert.ok(S.extra.length >= 1 && S.extra.length <= 4, `${S.extra.length} extra BVHs`);
  M.bindTrees(S);
  for (const c of spots) {
    const r = 3 * e0, got = new Set(M.facesNear(S, c, r)), want = new Set();
    for (let t = 0; t < S.nf; t++) if (M.touches(S.pos, S.idx, t, c, r)) want.add(t);
    assert.deepEqual([...got].sort((a, b) => a - b), [...want].sort((a, b) => a - b));
    // a ray from outside towards the centre lands on the refined surface, where a full search does
    const mesh = new THREE.Mesh(S.geom, new THREE.MeshBasicMaterial());
    mesh.raycast = M.sculptRaycast(S);
    const ray = new THREE.Raycaster(c.clone().multiplyScalar(3), c.clone().negate().normalize());
    ray.firstHitOnly = true;
    const hit = ray.intersectObject(mesh)[0], plain = new THREE.Mesh(S.geom.clone(), mesh.material);
    const full = new THREE.Raycaster(ray.ray.origin, ray.ray.direction).intersectObject(plain)[0];
    assert.ok(hit && Math.abs(hit.distance - full.distance) < 1e-6);
  }
});

test('normals after refining match a full recompute', () => {
  const S = refineAt(base, spots[3], 10 * e0, 4), g = S.geom.clone();
  g.computeVertexNormals();
  const full = g.attributes.normal.array;
  let worst = 0;
  for (let v = 0; v < S.nv; v++) worst = Math.max(worst, 1 - (S.nor[v * 3] * full[v * 3] + S.nor[v * 3 + 1] * full[v * 3 + 1] + S.nor[v * 3 + 2] * full[v * 3 + 2]));
  assert.ok(worst < 1e-4, `worst 1-cos ${worst}`);
});

test('refining leaves the other side of a thin part alone', () => {
  const S = plates(), e0 = M.medianEdge(S.pos, S.idx), c = new THREE.Vector3(0.5, 0.5, 0);
  const next = M.refine(S, c, 6 * e0, 4);  // the sphere reaches the far plate
  const T = M.grownState(S, next);
  for (let v = S.nv; v < T.nv; v++) assert.equal(T.pos[v * 3 + 2], 0, 'a vertex was added on the far plate');
});

test('×k is relative to the scan where you brush, also on a much coarser part of it', () => {
  // a scan whose top is ~3 times coarser than the rest (her_face's top of the head: 7-11 times its
  // median edge): ×1.5 there used to mean ×1.5 of the *typical* density, which gave ×60-115 there
  const out = M.decimate(base, [[0, 0, 1, 0.8]], 9), coarse = scan(out.pos, out.idx);
  let top = 0;
  for (let v = 0; v < coarse.nv; v++) if (coarse.pos[v * 3 + 2] > coarse.pos[top * 3 + 2]) top = v;
  const c = vec(coarse, top), e = coarse.density.at(c.x, c.y, c.z);
  assert.ok(e > 2 * M.medianEdge(coarse.pos, coarse.idx), `the top is not coarse enough to test (${e})`);
  const r = 6 * e, next = M.refine(coarse, c, 2 * r, 1.5), S = next ? M.grownState(coarse, next) : coarse;
  const got = near(S, c, r) / near(coarse, c, r);
  assert.ok(got > 1.1 && got < 2.2, `×1.5 gave ×${got.toFixed(1)}`);
});

test('after Decimate, ×k is relative to what it left, and a second Decimate simplifies further', () => {
  // the owner's choice on #37: ÷4 on the top of the head, then ×1.5 there, adds ~50%, not 6×
  const c = spots[0], R = 14 * e0, r = 8 * e0;
  const out = M.decimate(base, [[c.x, c.y, c.z, R]], 4);
  const dec = M.buildSculptState(out.pos, out.col8, out.idx, out.e0v, base.density);
  const once = refineAt(dec, c, r, 1.5), got = near(once, c, r) / near(dec, c, r);
  assert.ok(got > 1.1 && got < 2.2, `×1.5 after ÷4 gave ×${got.toFixed(2)}`);
  const twice = refineAt(once, c, r, 1.5);
  assert.ok(twice.nv - once.nv <= 0.05 * (once.nv - dec.nv) + 5, `brushing again added ${twice.nv - once.nv} more`);
  const half = M.decimate(base, [[c.x, c.y, c.z, R]], 2);
  const h1 = M.buildSculptState(half.pos, half.col8, half.idx, half.e0v, base.density);
  const out2 = M.decimate(h1, [[c.x, c.y, c.z, R]], 2), h2 = M.buildSculptState(out2.pos, out2.col8, out2.idx, out2.e0v, base.density);
  const n0 = near(base, c, r), n1 = near(h1, c, r), n2 = near(h2, c, r);
  assert.ok(n2 < 0.75 * n1, `÷2 twice: ${n0} → ${n1} → ${n2} vertices in the area`);
});
