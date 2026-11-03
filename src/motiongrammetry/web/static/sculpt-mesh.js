// The sculpt editor's mesh: its state (geometry, BVHs, adjacency) and the operations that change its
// density (Adaptive detail's refinement, Decimate). No DOM and no globals: edit.html calls these with
// its state object S, and tests/js runs them in Node on real meshes.
//
// Finding triangles: the main BVH covers the triangles the state was built with. Adaptive detail
// appends triangles; rebuilding the main BVH for them took ~0.5 s on a 400-600k face mesh, felt as a
// pause in the stroke (#4). Instead the appended ones go into small extra BVHs over their index range
// (S.extra), merged as they pile up (sizes at least double from one to the next, so there are few),
// and the main BVH is kept: a refined triangle keeps its slot and lies inside its old bounds. Use
// eachFaceNear / sculptRaycast / refitAll, which go through all of them.
import * as THREE from 'three';
import { MeshBVH, acceleratedRaycast } from 'three-mesh-bvh';

const BVH_OPTS = { maxLeafTris: 8 };

export function buildSculptState(pos, col8, index, e0v = null, density = null) {
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.BufferAttribute(pos, 3).setUsage(THREE.DynamicDrawUsage));
  geom.setAttribute('color', new THREE.BufferAttribute(col8, 3, true));
  geom.setIndex(new THREE.BufferAttribute(index, 1).setUsage(THREE.DynamicDrawUsage));
  geom.computeVertexNormals();
  geom.attributes.normal.setUsage(THREE.DynamicDrawUsage);
  const bvh = new MeshBVH(geom, BVH_OPTS);  // reorders the index buffer: read it afterwards
  geom.boundsTree = bvh;
  const nv = pos.length / 3, idx = geom.index.array;
  const st = { geom, bvh, extra: [], pos, nor: geom.attributes.normal.array, idx, col8, nv, nf: idx.length / 3,
    e0v: e0v || new Float32Array(nv).fill(NaN),
    ...adjacency(idx, nv), mark: new Uint32Array(nv), fmark: new Uint32Array(idx.length / 3), touch: new Uint32Array(nv) };
  if (density) st.density = density;
  return st;
}
export function adjacency(idx, nv) {
  const nf = idx.length / 3;
  const deg = new Uint32Array(nv + 1), fdeg = new Uint32Array(nv + 1);
  for (let i = 0; i < idx.length; i++) { deg[idx[i] + 1] += 2; fdeg[idx[i] + 1] += 1; }
  for (let i = 0; i < nv; i++) { deg[i + 1] += deg[i]; fdeg[i + 1] += fdeg[i]; }
  const adj = new Uint32Array(deg[nv]), vf = new Uint32Array(fdeg[nv]);
  const fa = deg.slice(0, nv), ff = fdeg.slice(0, nv);
  for (let t = 0; t < nf; t++) {
    const a = idx[t * 3], b = idx[t * 3 + 1], c = idx[t * 3 + 2];
    adj[fa[a]++] = b; adj[fa[a]++] = c; adj[fa[b]++] = a; adj[fa[b]++] = c; adj[fa[c]++] = a; adj[fa[c]++] = b;
    vf[ff[a]++] = t; vf[ff[b]++] = t; vf[ff[c]++] = t;
  }
  return { adjStart: deg, adj, vfStart: fdeg, vf };
}

// ---- the extra BVHs: one per range [start, end) of appended triangles, over a geometry that views
// the state's arrays (its BVH reorders that range of the index in place, which is fine: nothing
// refers to those triangles yet when it is built).
function chunk(S, start, end, bvh = null) {
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.BufferAttribute(S.pos, 3));
  geom.setIndex(new THREE.BufferAttribute(S.idx.subarray(start * 3, end * 3), 1));
  if (bvh) bvh.geometry = geom;
  return { start, end, geom, bvh: bvh || new MeshBVH(geom, BVH_OPTS) };
}
// After a state is (re)applied: its BVHs read its own arrays again (states share BVH objects).
export function bindTrees(S) {
  S.bvh.geometry = S.geom; S.geom.boundsTree = S.bvh;
  for (const x of S.extra) x.bvh.geometry = x.geom;
}
export function refitAll(S) {
  S.bvh.refit();
  for (const x of S.extra) x.bvh.refit();
}
const _sph = new THREE.Sphere();
// fn(t) for every triangle whose leaf box meets the sphere (c, r): a superset, check each one.
export function eachFaceNear(S, c, r, fn) {
  _sph.set(c, r);
  const hitBox = (box) => box.intersectsSphere(_sph);
  S.bvh.shapecast({ intersectsBounds: hitBox, intersectsRange: (off, count) => { for (let t = off; t < off + count; t++) fn(t); return false; } });
  for (const x of S.extra) {
    x.bvh.shapecast({ intersectsBounds: hitBox, intersectsRange: (off, count) => { for (let t = off; t < off + count; t++) fn(x.start + t); return false; } });
  }
}
// mesh.raycast for the sculpt mesh: the nearest hit over all its BVHs.
export function sculptRaycast(S) {
  return function (raycaster, intersects) {
    const own = this.geometry.boundsTree, hits = [];
    for (const tree of [S.bvh, ...S.extra.map((x) => x.bvh)]) {
      this.geometry.boundsTree = tree;
      acceleratedRaycast.call(this, raycaster, hits);
    }
    this.geometry.boundsTree = own;
    if (raycaster.firstHitOnly && hits.length) hits.sort((a, b) => a.distance - b.distance).length = 1;
    intersects.push(...hits);
  };
}

const _ta = new THREE.Vector3(), _tb = new THREE.Vector3(), _tc = new THREE.Vector3(), _tp = new THREE.Vector3(), _tri = new THREE.Triangle();
function dist2(p, f, t, c) {  // squared distance from c to triangle t (of index f, positions p)
  _ta.fromArray(p, f[t * 3] * 3); _tb.fromArray(p, f[t * 3 + 1] * 3); _tc.fromArray(p, f[t * 3 + 2] * 3);
  return _tri.set(_ta, _tb, _tc).closestPointToPoint(c, _tp).distanceToSquared(c);
}
export const touches = (p, f, t, c, r) => dist2(p, f, t, c) <= r * r;
const emptied = (f, t) => f[t * 3] === f[t * 3 + 1] && f[t * 3 + 1] === f[t * 3 + 2];  // a cut collapses a triangle
export function facesNear(S, c, r) {  // triangles that come within r of c (not the ones a cut emptied)
  const { pos, idx } = S, out = [];
  eachFaceNear(S, c, r, (t) => { if (!emptied(idx, t) && touches(pos, idx, t, c, r)) out.push(t); });
  return out;
}
// The triangles near c that are on the same surface as c: connected (through shared vertices, inside
// the sphere) to the triangle nearest c. A sphere twice the brush also reaches other sides of a thin
// part, or the inside of a shoe, which the brush is not working on.
export function surfaceNear(S, c, r) {
  const near = facesNear(S, c, r);
  if (near.length < 2) return near;
  const { pos, idx, vfStart, vf, nf } = S, inSet = new Set(near);
  let seed = near[0], best = Infinity;
  for (const t of near) { const d = dist2(pos, idx, t, c); if (d < best) { best = d; seed = t; } }
  const seen = new Set([seed]), out = [seed];
  for (let i = 0; i < out.length; i++) {
    const t = out[i];
    for (let k = 0; k < 3; k++) {
      const v = idx[t * 3 + k];
      for (let j = vfStart[v]; j < vfStart[v + 1]; j++) {
        const u = vf[j];
        if (u < nf && inSet.has(u) && !seen.has(u)) { seen.add(u); out.push(u); }
      }
    }
  }
  return out;
}
export function longest(p, f, t) {  // [squared length, a, b] of triangle t's longest edge
  let best = [-1, 0, 0];
  for (let k = 0; k < 3; k++) {
    const a = f[t * 3 + k], b = f[t * 3 + (k + 1) % 3];
    const d = (p[a * 3] - p[b * 3]) ** 2 + (p[a * 3 + 1] - p[b * 3 + 1]) ** 2 + (p[a * 3 + 2] - p[b * 3 + 2]) ** 2;
    if (d > best[0]) best = [d, a, b];
  }
  return best;
}
// Median edge length (from a sample of up to ~20k triangles): the density measure for a mesh.
export function medianEdge(pos, idx) {
  const nf = idx.length / 3, step = Math.max(1, Math.floor(nf / 20000)), e = [];
  for (let t = 0; t < nf; t += step) {
    if (emptied(idx, t)) continue;
    for (let k = 0; k < 3; k++) {
      const a = idx[t * 3 + k] * 3, b = idx[t * 3 + (k + 1) % 3] * 3;
      e.push(Math.hypot(pos[a] - pos[b], pos[a + 1] - pos[b + 1], pos[a + 2] - pos[b + 2]));
    }
  }
  if (!e.length) return 0;
  e.sort((x, y) => x - y);
  return e[e.length >> 1];
}

// ---- density: ×k means k times the vertices (and faces) the original scan had where you brush,
// edges √k shorter. "Where you brush": a scan is often much coarser in some places than others
// (her_face's top of the head has edges 7-11 times its median), so each vertex gets the scan's own
// edge length at its place (S.e0v, looked up in S.density, the pipeline's clean mesh, on first use;
// a new vertex inherits the mean of the edge it splits). A factor of the scan's typical density
// instead turned ×1.5 into ×60-115 on those coarse patches. The same factor gives the same result at
// any zoom or brush size, and brushing an area again adds (almost) nothing: the target comes from
// the original scan, so it is a ceiling and doesn't compound.
// SPLIT and MERGE were calibrated on the shoe, orbit, plant and her_face (see the PR of #13);
// tests/js/sculpt-mesh.test.mjs checks the result.
export const SPLIT = 1.5;   // Adaptive detail splits edges longer than SPLIT · e/√k
export const MERGE = 0.68;  // Decimate merges edges shorter than MERGE · e·√k
// The scan's density: each vertex's edge length (the mean of its edges, then averaged once with its
// neighbours'), found for any point through the nearest triangle.
export class ScanDensity {
  constructor(pos, idx) {
    const nv = pos.length / 3, sum = new Float64Array(nv), cnt = new Uint32Array(nv);
    for (let t = 0; t < idx.length / 3; t++) for (let k = 0; k < 3; k++) {
      const a = idx[t * 3 + k], b = idx[t * 3 + (k + 1) % 3];
      const l = Math.hypot(pos[a * 3] - pos[b * 3], pos[a * 3 + 1] - pos[b * 3 + 1], pos[a * 3 + 2] - pos[b * 3 + 2]);
      sum[a] += l; cnt[a]++; sum[b] += l; cnt[b]++;
    }
    const L = new Float32Array(nv), s2 = new Float64Array(nv), c2 = new Uint32Array(nv);
    for (let v = 0; v < nv; v++) L[v] = cnt[v] ? sum[v] / cnt[v] : 0;
    for (let t = 0; t < idx.length / 3; t++) for (let k = 0; k < 3; k++) {
      const a = idx[t * 3 + k], b = idx[t * 3 + (k + 1) % 3];
      s2[a] += L[b]; c2[a]++; s2[b] += L[a]; c2[b]++;
    }
    for (let v = 0; v < nv; v++) if (c2[v]) L[v] = (L[v] + s2[v] / c2[v]) / 2;
    this.L = L;
    this.geom = new THREE.BufferGeometry();
    this.geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    this.geom.setIndex(new THREE.BufferAttribute(idx, 1));
    this.bvh = new MeshBVH(this.geom, BVH_OPTS);  // reorders this.geom's index, read through it below
    this._p = new THREE.Vector3(); this._hit = {};
  }
  at(x, y, z) {
    const h = this.bvh.closestPointToPoint(this._p.set(x, y, z), this._hit), f = this.geom.index.array, t = h.faceIndex;
    return (this.L[f[t * 3]] + this.L[f[t * 3 + 1]] + this.L[f[t * 3 + 2]]) / 3;
  }
}
// The reference edge of vertex v (positions p, reference edges E), looked up on first use.
function edgeAt(S, p, E, v) {
  let l = E[v];
  if (!(l > 0)) E[v] = l = S.density.at(p[v * 3], p[v * 3 + 1], p[v * 3 + 2]);
  return l;
}
export function needsDetail(S, c, r, k) {
  const f = (SPLIT / Math.sqrt(k)) ** 2;
  return facesNear(S, c, r).some((t) => {
    const [d, a, b] = longest(S.pos, S.idx, t), l = (edgeAt(S, S.pos, S.e0v, a) + edgeAt(S, S.pos, S.e0v, b)) / 2;
    return d > f * l * l;
  });
}
export class Grow {  // a typed array with room to append
  constructor(src, extra) { this.a = new src.constructor(src.length + extra); this.a.set(src); this.n = src.length; }
  room(k) { if (this.n + k > this.a.length) { const b = new this.a.constructor(Math.ceil((this.n + k) * 1.5)); b.set(this.a.subarray(0, this.n)); this.a = b; } }
  push(...xs) { this.room(xs.length); for (const x of xs) this.a[this.n++] = x; }
  out() { return this.a.slice(0, this.n); }
}
// Longest-edge bisection: every triangle of the surface within R of c whose longest edge is longer
// than SPLIT · e/√k (e: the scan's edge there) has that edge split at its middle, round after round,
// until none is. A triangle with 1, 2 or 3 split edges becomes 2, 3 or 4 (its first piece keeps its
// slot), and the triangles across a split edge are split too, so no crack opens. It stops adding
// vertices at `budget`. Returns { pos, col8, idx, e0v, added, changed (slots < nf that now hold a
// piece), capped }, or null.
export function refine(S, c, R, k, budget = Infinity) {
  const nv0 = S.nv, nf0 = S.nf, { vfStart, vf } = S, f2 = (SPLIT / Math.sqrt(k)) ** 2;
  const P = new Grow(S.pos, 3 * 4096), C = new Grow(S.col8, 3 * 4096), F = new Grow(S.idx, 3 * 8192), E = new Grow(S.e0v, 4096);
  const KEY = 2 ** 24, key = (a, b) => (a < b ? a * KEY + b : b * KEY + a);
  const changed = new Set(), written = new Map();  // vertex -> triangles this call wrote with it
  const wrote = (t, tris, i) => { for (let k = i; k < i + 3; k++) { const v = tris[k]; let w = written.get(v); if (!w) written.set(v, w = []); w.push(t); } };
  let active = surfaceNear(S, c, R), added = 0, capped = false;
  for (let pass = 0; pass < 40 && active.length && !capped; pass++) {
    const mid = new Map(), ends = [];
    const p = P.a, f = F.a;
    for (const t of active) {
      const [d, a, b] = longest(p, f, t);
      const l = (edgeAt(S, p, E.a, a) + edgeAt(S, p, E.a, b)) / 2;
      if (d <= f2 * l * l || !touches(p, f, t, c, R)) continue;
      const ka = key(a, b);
      if (mid.has(ka)) continue;
      if (added + mid.size >= budget) { capped = true; break; }
      const mx = (p[a * 3] + p[b * 3]) / 2, my = (p[a * 3 + 1] + p[b * 3 + 1]) / 2, mz = (p[a * 3 + 2] + p[b * 3 + 2]) / 2;
      const m = P.n / 3;
      P.push(mx, my, mz);
      const cc = C.a;
      C.push((cc[a * 3] + cc[b * 3]) >> 1, (cc[a * 3 + 1] + cc[b * 3 + 1]) >> 1, (cc[a * 3 + 2] + cc[b * 3 + 2]) >> 1);
      E.push(l);
      mid.set(ka, m); ends.push(a, b);
    }
    if (!mid.size) break;
    added += mid.size;
    // the triangles to split: the active ones, and every triangle across a split edge, found among
    // the original ones (vf) and the ones written in earlier passes: one made two passes ago and
    // left alone since is in neither `active` nor vf, and missing it left a crack (a T-junction)
    // that the save's cleanup then cut out as bow-tie vertices.
    const todo = new Set(active), ff = F.a;
    const has = (t, v) => ff[t * 3] === v || ff[t * 3 + 1] === v || ff[t * 3 + 2] === v;
    for (let i = 0; i < ends.length; i += 2) {
      const a = ends[i], b = ends[i + 1];
      if (a < nv0) for (let j = vfStart[a]; j < vfStart[a + 1]; j++) { const t = vf[j]; if (has(t, a) && has(t, b)) todo.add(t); }
      const w = written.get(a);
      if (w) for (const t of w) if (has(t, a) && has(t, b)) todo.add(t);
    }
    const next = [];
    for (const t of todo) {
      const ff = F.a, a = ff[t * 3], b = ff[t * 3 + 1], cc = ff[t * 3 + 2];
      const ab = mid.get(key(a, b)), bc = mid.get(key(b, cc)), ca = mid.get(key(cc, a));
      const n = (ab !== undefined) + (bc !== undefined) + (ca !== undefined);
      if (!n) continue;  // not split: it did not qualify, and it will not later (it has not changed)
      let tris;
      if (n === 3) tris = [a, ab, ca, ab, b, bc, ca, bc, cc, ab, bc, ca];
      else {
        // turn the triangle (x, y, z) so its split edges come first: x-y (and y-z)
        const rot = n === 1 ? (ab !== undefined ? 0 : bc !== undefined ? 1 : 2)
          : (ab !== undefined && bc !== undefined ? 0 : bc !== undefined && ca !== undefined ? 1 : 2);
        const v = [a, b, cc], m = [ab, bc, ca];
        const x = v[rot], y = v[(rot + 1) % 3], z = v[(rot + 2) % 3], mxy = m[rot], myz = m[(rot + 1) % 3];
        tris = n === 1 ? [x, mxy, z, mxy, y, z] : [y, myz, mxy, x, mxy, myz, x, myz, z];
      }
      F.a[t * 3] = tris[0]; F.a[t * 3 + 1] = tris[1]; F.a[t * 3 + 2] = tris[2];
      if (t < nf0) changed.add(t);
      next.push(t); wrote(t, tris, 0);
      for (let i = 3; i < tris.length; i += 3) { const u = F.n / 3; next.push(u); F.push(tris[i], tris[i + 1], tris[i + 2]); wrote(u, tris, i); }
    }
    active = next;
  }
  return added ? { pos: P.out(), col8: C.out(), idx: F.out(), e0v: E.out(), added, changed: [...changed], capped } : null;
}
// The state after refine(S, …): same vertex ids and triangle slots, more of both. Unlike
// buildSculptState it keeps the main BVH (refined slots lie inside their old bounds) and the normals
// away from the change, and puts the new triangles in an extra BVH.
export function grownState(S, next) {
  const { pos, col8, idx, e0v, changed } = next, nv = pos.length / 3, nf = idx.length / 3, nf0 = S.nf;
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.BufferAttribute(pos, 3).setUsage(THREE.DynamicDrawUsage));
  geom.setAttribute('color', new THREE.BufferAttribute(col8, 3, true));
  geom.setIndex(new THREE.BufferAttribute(idx, 1).setUsage(THREE.DynamicDrawUsage));
  const nor = new Float32Array(nv * 3); nor.set(S.nor);
  geom.setAttribute('normal', new THREE.BufferAttribute(nor, 3).setUsage(THREE.DynamicDrawUsage));
  const st = { geom, bvh: S.bvh, pos, nor, idx, col8, e0v, nv, nf,
    mark: new Uint32Array(nv), fmark: new Uint32Array(nf), touch: new Uint32Array(nv) };
  st.touch.set(S.touch);
  if (S.density) st.density = S.density;
  // the vertices whose normal changes: those of every triangle that changed or is new
  const around = new Set();
  for (const t of changed) for (let k = 0; k < 3; k++) around.add(idx[t * 3 + k]);
  for (let t = nf0; t < nf; t++) for (let k = 0; k < 3; k++) around.add(idx[t * 3 + k]);
  // BVHs: the earlier extra ones (on this state's arrays), the new range, merged while the one before
  // is no more than twice its size. Building one reorders its range of the index: adjacency after.
  st.extra = S.extra.map((x) => chunk(st, x.start, x.end, x.bvh));
  let start = nf0;
  while (st.extra.length && st.extra.at(-1).end - st.extra.at(-1).start <= 2 * (nf - start)) start = st.extra.pop().start;
  st.extra.push(chunk(st, start, nf));
  Object.assign(st, adjacency(idx, nv));
  for (const v of around) vertexNormal(st, v);
  st.bvh.geometry = geom; geom.boundsTree = st.bvh;
  return st;
}
export function vertexNormal(st, v) {
  const { pos, nor, idx, vfStart, vf } = st;
  let nx = 0, ny = 0, nz = 0;
  for (let j = vfStart[v]; j < vfStart[v + 1]; j++) {
    const f = vf[j], a = idx[f * 3] * 3, b = idx[f * 3 + 1] * 3, c = idx[f * 3 + 2] * 3;
    const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2];
    const wx = pos[c] - pos[a], wy = pos[c + 1] - pos[a + 1], wz = pos[c + 2] - pos[a + 2];
    nx += uy * wz - uz * wy; ny += uz * wx - ux * wz; nz += ux * wy - uy * wx;
  }
  const l = Math.hypot(nx, ny, nz) || 1;
  nor[v * 3] = nx / l; nor[v * 3 + 1] = ny / l; nor[v * 3 + 2] = nz / l;
}
// Rebuild everything (one BVH) when the extra BVHs hold as many triangles as the main one.
export const wantsRebuild = (S) => S.extra.reduce((n, x) => n + x.end - x.start, 0) > S.nf - S.extra.reduce((n, x) => n + x.end - x.start, 0);

// Merge the edges shorter than MERGE · e·√k (e: the scan's edge there) inside the dabs ([x, y, z, r] each), shortest first. A merge is
// skipped when it would fold a triangle or pinch the surface (the two ends share more neighbours
// than the triangles on the edge); vertices on the mesh's open edges and on the dabs' border stay
// put, so nothing outside changes. Returns the compacted arrays and face counts, or null.
export function decimate(S, dabs, k) {
  const { pos, col8, idx, vfStart, vf } = S, nf0 = idx.length / 3, E = S.e0v.slice();  // the state before stays as it was (undo)
  const P = pos.slice(), F = idx.slice(), dead = new Uint8Array(nf0);
  const inside = new Set();
  for (const [x, y, z, r] of dabs) for (const t of facesNear(S, new THREE.Vector3(x, y, z), r)) inside.add(t);
  if (!inside.size) return null;
  const faces = new Map();  // vertex -> Set of its live faces (only the vertices involved)
  const facesOf = (v) => {
    let s = faces.get(v);
    if (!s) { s = new Set(); for (let j = vfStart[v]; j < vfStart[v + 1]; j++) { const t = vf[j]; if (!dead[t] && (F[t * 3] === v || F[t * 3 + 1] === v || F[t * 3 + 2] === v)) s.add(t); } faces.set(v, s); }
    return s;
  };
  const ring = (v) => { const n = new Set(); for (const t of facesOf(v)) for (let k = 0; k < 3; k++) if (F[t * 3 + k] !== v) n.add(F[t * 3 + k]); return n; };
  const locked = new Map();
  const isLocked = (v) => {
    if (locked.has(v)) return locked.get(v);
    let lock = false;
    for (const t of facesOf(v)) if (!inside.has(t)) { lock = true; break; }  // on the dabs' border
    if (!lock) {  // on an open edge: an edge of v with one face
      const count = new Map();
      for (const t of facesOf(v)) for (let k = 0; k < 3; k++) { const u = F[t * 3 + k]; if (u !== v) count.set(u, (count.get(u) || 0) + 1); }
      for (const n of count.values()) if (n === 1) { lock = true; break; }
    }
    locked.set(v, lock);
    return lock;
  };
  const len2 = (a, b) => (P[a * 3] - P[b * 3]) ** 2 + (P[a * 3 + 1] - P[b * 3 + 1]) ** 2 + (P[a * 3 + 2] - P[b * 3 + 2]) ** 2;
  const f2 = MERGE * MERGE * k;
  const lim2of = (a, b) => { const l = (edgeAt(S, P, E, a) + edgeAt(S, P, E, b)) / 2; return f2 * l * l; };
  const normal = (t, swapFrom = -1, swapTo = -1, at = null) => {
    const v = [0, 1, 2].map((k) => F[t * 3 + k]), q = v.map((u) => (u === swapFrom ? at : [P[u * 3], P[u * 3 + 1], P[u * 3 + 2]]));
    const ux = q[1][0] - q[0][0], uy = q[1][1] - q[0][1], uz = q[1][2] - q[0][2], wx = q[2][0] - q[0][0], wy = q[2][1] - q[0][1], wz = q[2][2] - q[0][2];
    return [uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx];
  };
  let merged = 0;
  for (let pass = 0; pass < 8; pass++) {
    const edges = [];
    for (const t of inside) if (!dead[t]) for (let k = 0; k < 3; k++) {
      const a = F[t * 3 + k], b = F[t * 3 + (k + 1) % 3];
      if (a < b) { const d = len2(a, b); if (d < lim2of(a, b)) edges.push([d, a, b]); }
    }
    if (!edges.length) break;
    edges.sort((x, y) => x[0] - y[0]);
    let did = 0;
    for (const [, a0, b0] of edges) {
      let a = a0, b = b0;  // b goes, a stays (moved to the middle unless one end is locked)
      const la = isLocked(a), lb = isLocked(b);
      if (la && lb) continue;
      if (lb) [a, b] = [b, a];
      const fa = facesOf(a), fb = facesOf(b);
      if (!fa.size || !fb.size || len2(a, b) >= lim2of(a, b)) continue;
      const shared = [...fb].filter((t) => fa.has(t));
      if (!shared.length) continue;  // no longer an edge
      const ra = ring(a), rb = ring(b), common = [...ra].filter((u) => rb.has(u));
      if (common.length !== shared.length) continue;  // would pinch the surface
      const at = la || lb ? [P[a * 3], P[a * 3 + 1], P[a * 3 + 2]]
        : [(P[a * 3] + P[b * 3]) / 2, (P[a * 3 + 1] + P[b * 3 + 1]) / 2, (P[a * 3 + 2] + P[b * 3 + 2]) / 2];
      let flips = false;
      for (const t of [...fa, ...fb]) {
        if (shared.includes(t)) continue;
        const n0 = normal(t), moved = fb.has(t) ? normal(t, b, a, at) : normal(t, a, a, at);
        if (n0[0] * moved[0] + n0[1] * moved[1] + n0[2] * moved[2] <= 0) { flips = true; break; }
      }
      if (flips) continue;
      for (const t of shared) { dead[t] = 1; for (let k = 0; k < 3; k++) facesOf(F[t * 3 + k]).delete(t); }
      for (const t of fb) { for (let k = 0; k < 3; k++) if (F[t * 3 + k] === b) F[t * 3 + k] = a; fa.add(t); }
      fb.clear();
      P[a * 3] = at[0]; P[a * 3 + 1] = at[1]; P[a * 3 + 2] = at[2];
      if (!la && !lb) E[a] = (edgeAt(S, P, E, a) + edgeAt(S, P, E, b)) / 2;
      for (const u of [a, ...ra]) locked.delete(u);
      merged++; did++;
    }
    if (!did) break;
  }
  if (!merged) return null;
  // compact: live faces, the vertices they use
  const map = new Int32Array(S.nv).fill(-1);
  let nv = 0, nf = 0;
  for (let t = 0; t < nf0; t++) if (!dead[t] && !emptied(F, t))
    for (let k = 0; k < 3; k++) { const v = F[t * 3 + k]; if (map[v] < 0) map[v] = nv++; }
  // the vertices Decimate worked on: in the dabs and free to move (not on their border or an open edge)
  const worked = new Uint8Array(nv);
  for (const t of inside) for (let j = 0; j < 3; j++) {
    const u = F[t * 3 + j];
    if (map[u] >= 0 && !worked[map[u]] && !isLocked(u)) { worked[map[u]] = 1; edgeAt(S, P, E, u); }
  }
  const np = new Float32Array(nv * 3), nc = new Uint8Array(nv * 3), ne = new Float32Array(nv);
  for (let v = 0; v < S.nv; v++) if (map[v] >= 0) { np.set(P.subarray(v * 3, v * 3 + 3), map[v] * 3); nc.set(col8.subarray(v * 3, v * 3 + 3), map[v] * 3); ne[map[v]] = E[v]; }
  const ni = [];
  for (let t = 0; t < nf0; t++) if (!dead[t] && !emptied(F, t)) { ni.push(map[F[t * 3]], map[F[t * 3 + 1]], map[F[t * 3 + 2]]); nf++; }
  // What Decimate leaves becomes the reference there (the owner's choice, on #37): Adaptive detail's
  // ×k afterwards is relative to it, not to the denser scan, and a second Decimate simplifies further.
  // Each vertex it worked on takes the longer of its reference edge and the mean of its edges now, so
  // where nothing was merged the reference stays the scan's.
  const sum = new Float64Array(nv), cnt = new Uint32Array(nv);
  for (let i = 0; i < ni.length; i += 3) for (let j = 0; j < 3; j++) {
    const a = ni[i + j], b = ni[i + (j + 1) % 3];
    if (!worked[a] && !worked[b]) continue;
    const l = Math.hypot(np[a * 3] - np[b * 3], np[a * 3 + 1] - np[b * 3 + 1], np[a * 3 + 2] - np[b * 3 + 2]);
    if (worked[a]) { sum[a] += l; cnt[a]++; }
    if (worked[b]) { sum[b] += l; cnt[b]++; }
  }
  for (let v = 0; v < nv; v++) if (worked[v] && cnt[v]) ne[v] = Math.max(ne[v], sum[v] / cnt[v]);
  return { pos: np, col8: nc, idx: Uint32Array.from(ni), e0v: ne, before: nf0, after: nf };
}
