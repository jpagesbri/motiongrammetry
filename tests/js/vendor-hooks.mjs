// Resolve the browser's import map in Node: 'three' and 'three-mesh-bvh' are the vendored copies the
// editor loads. Used as: node --import ./tests/js/vendor-hooks.mjs --test tests/js/*.test.mjs
import { registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const STATIC = resolve(import.meta.dirname, '../../src/motiongrammetry/web/static');
const MAP = {
  'three': `${STATIC}/vendor/three/build/three.module.js`,
  'three-mesh-bvh': `${STATIC}/vendor/three-mesh-bvh/build/index.module.js`,
};
registerHooks({
  resolve(spec, ctx, next) {
    if (MAP[spec]) return { url: pathToFileURL(MAP[spec]).href, shortCircuit: true, format: 'module' };
    if (spec.startsWith('three/addons/')) return { url: pathToFileURL(`${STATIC}/vendor/three/examples/jsm/${spec.slice(13)}`).href, shortCircuit: true, format: 'module' };
    return next(spec, ctx);
  },
});
