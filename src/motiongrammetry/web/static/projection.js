// Perspective or orthographic, for every 3D view: the Scan preview, the editor and the Export
// window's preview (export.js). The view cube (viewcube.js) shows its Persp / Ortho button.
import * as THREE from 'three';

// Perspective or orthographic view of the same orbit: the orthographic camera frames what the
// perspective one sees at the orbit target and zooms instead of moving. Code that places the
// camera (fit, fly to a keyframe) moves the perspective one, then framed() brings the other along.
export function projection(persp, controls, light, onSwitch) {
  const rad = THREE.MathUtils.degToRad, ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, persp.near, persp.far);
  persp.parent?.add(ortho);
  const P = {
    camera: persp, aspect: persp.aspect,
    set(on) {
      if (on === !!P.camera.isOrthographicCamera) return;
      const t = controls.target;
      if (on) {
        const half = persp.position.distanceTo(t) * Math.tan(rad(persp.fov / 2));
        Object.assign(ortho, { top: half, bottom: -half, left: -half * P.aspect, right: half * P.aspect, zoom: 1, near: persp.near, far: persp.far });
        ortho.position.copy(persp.position); ortho.quaternion.copy(persp.quaternion); ortho.up.copy(persp.up);
        ortho.updateProjectionMatrix(); P.camera = ortho;
      } else {
        const d = ortho.top / ortho.zoom / Math.tan(rad(persp.fov / 2)), dir = ortho.position.clone().sub(t).normalize();
        persp.position.copy(t).addScaledVector(dir, d); persp.quaternion.copy(ortho.quaternion); persp.up.copy(ortho.up);
        persp.updateProjectionMatrix(); P.camera = persp;
      }
      if (light) P.camera.add(light);
      controls.object = P.camera; controls.update();
      onSwitch?.(P.camera);
    },
    resize(aspect) {
      P.aspect = aspect; persp.aspect = aspect; persp.updateProjectionMatrix();
      ortho.left = -ortho.top * aspect; ortho.right = ortho.top * aspect; ortho.updateProjectionMatrix();
    },
    framed() { if (P.camera === ortho) { P.camera = persp; P.set(true); } else controls.update(); },
    pxPerUnit(h, dist) {  // screen pixels per scene unit at the orbit target
      return P.camera === ortho ? h * ortho.zoom / (ortho.top - ortho.bottom) : h / (2 * dist * Math.tan(rad(persp.fov / 2)));
    },
  };
  return P;
}
