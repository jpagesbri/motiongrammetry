// How the mouse and keys move every 3D view: the Scan page's preview, the editor and the Export
// window's preview. Left or right drag turns about the target, middle drag pans, the wheel zooms;
// Space held + left drag pans too. The clipping planes follow the camera, so nothing is cut when
// you come close. Where each page starts and what its frame button does stay its own.
//
//   const nav = navigation(camera, canvas);   // OrbitControls on it, set up as above
//   nav.controls                               // for the view cube, projection(), code that moves the camera
//   nav.frame(size, camera)                    // after framing something `size` across (its diagonal)
//   nav.clip()                                 // before each render: near / far from the distance to the target
//   spaceHeld()                                // Space is down (the editor's flat texture view pans with it too)
//
// A page that wants the left button for itself (the editor's strokes) listens on the canvas with
// { capture: true } before calling navigation(), and stops the event when it takes it.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const views = new Set();
let space = false;

export const spaceHeld = () => space;

const typing = (t) => !!t && (t.isContentEditable || (t.matches && t.matches('input, select, textarea')));
function setSpace(on) {
  if (space === on) return;
  space = on;
  views.forEach((v) => { v.controls.mouseButtons.LEFT = on ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE; });
}
let listening = false;
function listen() {
  if (listening) return;
  listening = true;
  // Capture on the window: before the pages' own key handlers (the Export window stops the keys it gets).
  // Over a 3D view, Space neither scrolls the page nor presses the focused button.
  addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    setSpace(true);
    if ([...views].some((v) => v.hover)) e.preventDefault();
  }, true);
  addEventListener('keyup', (e) => {
    if (e.code !== 'Space') return;
    if (space && [...views].some((v) => v.hover)) e.preventDefault();
    setSpace(false);
  }, true);
  addEventListener('blur', () => setSpace(false));  // the key went up in another window
}

export function navigation(camera, dom) {
  listen();
  const controls = new OrbitControls(camera, dom);
  controls.enableDamping = false;  // the views render on demand only
  controls.mouseButtons = { LEFT: space ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.ROTATE };
  const nav = {
    controls,
    size: 1,     // the size of what was framed last
    hover: false,
    frame(size, cam = controls.object) { nav.size = size || 1; nav.clip(cam); cam.updateProjectionMatrix(); },
    // Near and far from the camera's distance to the target, scaled by what was framed. Fixed once
    // when framing, they kept that scale: a model approached closely was cut by the near plane (#22).
    clip(cam = controls.object) {
      if (!cam.isPerspectiveCamera) return;
      const d = cam.position.distanceTo(controls.target);
      const near = Math.max(d / 1000, nav.size / 1e5), far = d + nav.size * 20;
      if (cam.near !== near || cam.far !== far) { cam.near = near; cam.far = far; cam.updateProjectionMatrix(); }
    },
  };
  dom.addEventListener('pointerenter', () => { nav.hover = true; });
  dom.addEventListener('pointerleave', () => { nav.hover = false; });
  dom.addEventListener('contextmenu', (e) => e.preventDefault());  // right drag turns, even where the controls are off
  views.add(nav);
  dom.navigation = nav;  // for the browser tests
  return nav;
}
