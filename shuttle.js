// Shuttle drop shared by index.html and tripods.html.
// Landing points use court metres with the net centre as origin:
// X across the width, Z along the length (same as the 3D scene).
import * as THREE from 'three';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const HALF_W = 6.10 / 2;
const HALF_L = 13.40 / 2;
const SINGLES_WIDTH = 5.18;

const LAND_Y = 0.056;            // just above the painted lines
const SHUTTLE_SCALE = 3;         // a real shuttle (~8.5 cm) is too small to see from the camera
const CORK_R = 0.0125 * SHUTTLE_SCALE;
const REGION_X = HALF_W + 0.55;  // clickable/droppable area = court + coloured border
const REGION_Z = HALF_L + 0.55;
const UP = new THREE.Vector3(0, 1, 0);
const IN_COLOR = 0x5ff0b0, OUT_COLOR = 0xff7a5f;

// Drop: a short, low fall right onto the spot, then a small bounce.
const DROP_START_Y = 0.30;       // metres above the floor where the shuttle starts
const DROP_BACK = 0.25;          // metres it travels sideways while falling
const FALL_T = 0.32, B1_T = 0.30, B2_T = 0.18, SETTLE_T = 0.25;   // seconds
const B1_H = 0.20, B2_H = 0.06;  // bounce heights, metres

const PANEL_HTML = `
  <p class="panel-title">Shuttle drop</p>
  <div class="seg" role="tablist">
    <button type="button" id="modePoint" class="active" aria-pressed="true">Point</button>
    <button type="button" id="modeCoord" aria-pressed="false">Co-ordinates</button>
  </div>
  <p class="hint" id="pointHint">Click anywhere on the court to drop a shuttle there.</p>
  <form id="coordForm" class="coord-form" hidden>
    <label>X (width, m) <input id="inX" type="number" step="0.01" value="1.20" required /></label>
    <label>Z (length, m) <input id="inZ" type="number" step="0.01" value="-4.50" required /></label>
    <small>Origin (0, 0) = net centre.<br />X: −3.05 … 3.05 &nbsp;·&nbsp; Z: −6.70 … 6.70</small>
    <button type="submit">Drop shuttle</button>
    <div class="err" id="coordErr"></div>
  </form>
  <div class="result" id="result" hidden>
    <div><span>Landing X</span><strong id="rX">–</strong></div>
    <div><span>Landing Z</span><strong id="rZ">–</strong></div>
    <div><span>Doubles</span><strong id="rD">–</strong></div>
    <div><span>Singles</span><strong id="rS">–</strong></div>
  </div>
  <button type="button" class="ghost" id="replayBtn" hidden>Replay recorded fall</button>
  <button type="button" class="ghost" id="clear">Clear marks</button>`;

export function judge(x, z) {
  const inLength = Math.abs(z) <= HALF_L;   // lines count as in
  return {
    doubles: inLength && Math.abs(x) <= HALF_W,
    singles: inLength && Math.abs(x) <= SINGLES_WIDTH / 2,
  };
}

// Builds the shuttle, its panel (inside `panelEl`) and click-to-drop on the court.
// `onStatus(text)` receives status-bar messages. Call `update(now)` every frame.
// `onReplay(fall, landing)` is called when a recorded fall starts replaying.
export function setupShuttleDrop({ scene, camera, renderer, panelEl, onStatus = () => {}, onReplay = () => {} }) {
  panelEl.innerHTML = PANEL_HTML;
  const $ = id => document.getElementById(id);

  // Shuttle model, origin at the bottom of the cork, skirt pointing +Y.
  const shuttle = new THREE.Group();
  {
    const r = 0.0125;
    const cork = new THREE.Mesh(
      new THREE.SphereGeometry(r, 20, 10, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2),
      new THREE.MeshStandardMaterial({ color: 0xe9dcc0, roughness: 0.7 })
    );
    cork.position.y = r;
    const band = new THREE.Mesh(
      new THREE.CylinderGeometry(r, r, 0.007, 20),
      new THREE.MeshStandardMaterial({ color: 0x1f8f74, roughness: 0.5 })
    );
    band.position.y = r + 0.0035;
    const featherMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.6, side: THREE.DoubleSide, transparent: true, opacity: 0.92 });
    const skirt = new THREE.Mesh(new THREE.CylinderGeometry(0.034, 0.013, 0.07, 28, 1, true), featherMat);
    skirt.position.y = r + 0.007 + 0.035;
    const threadMat = new THREE.MeshStandardMaterial({ color: 0xcfd8d4, roughness: 0.6 });
    const rim = new THREE.Mesh(new THREE.TorusGeometry(0.034, 0.0012, 6, 32), threadMat);
    rim.rotation.x = Math.PI / 2;
    rim.position.y = r + 0.007 + 0.07;
    const thread = new THREE.Mesh(new THREE.TorusGeometry(0.0235, 0.0009, 6, 32), threadMat);
    thread.rotation.x = Math.PI / 2;
    thread.position.y = r + 0.007 + 0.035;
    for (const m of [cork, band, skirt, rim, thread]) { m.castShadow = true; shuttle.add(m); }
  }
  shuttle.scale.setScalar(SHUTTLE_SCALE);
  shuttle.visible = false;
  scene.add(shuttle);

  // Hover preview ring for Point mode.
  const hoverRing = new THREE.Mesh(
    new THREE.RingGeometry(0.1, 0.13, 40),
    new THREE.MeshBasicMaterial({ color: 0xbff7e6, transparent: true, opacity: 0.55, depthWrite: false })
  );
  hoverRing.rotation.x = -Math.PI / 2;
  hoverRing.position.y = LAND_Y + 0.003;
  hoverRing.visible = false;
  scene.add(hoverRing);

  const markers = [];   // persistent landing marks
  const ripples = [];   // short-lived impact rings
  let landingTag = null;

  function addMarker(x, z, isIn) {
    for (const m of markers) m.traverse(o => { if (o.material) o.material.opacity *= 0.6; });
    const color = isIn ? IN_COLOR : OUT_COLOR;
    const g = new THREE.Group();
    const dot = new THREE.Mesh(new THREE.CircleGeometry(0.09, 32),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.95, depthWrite: false }));
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.16, 0.2, 48),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.85, depthWrite: false }));
    dot.rotation.x = ring.rotation.x = -Math.PI / 2;
    g.add(dot, ring);
    g.position.set(x, LAND_Y + 0.002, z);
    scene.add(g);
    markers.push(g);
  }

  function addRipple(x, z, color, delay) {
    const mesh = new THREE.Mesh(new THREE.RingGeometry(0.9, 1, 64),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0, depthWrite: false }));
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(x, LAND_Y + 0.004, z);
    scene.add(mesh);
    ripples.push({ mesh, born: performance.now() + delay });
  }

  function setTag(x, z, res) {
    if (landingTag) { scene.remove(landingTag); landingTag.element.remove(); }
    const el = document.createElement('div');
    el.className = 'land-tag' + (res.doubles ? '' : ' out');
    el.textContent = `(${x.toFixed(2)}, ${z.toFixed(2)}) · ${res.doubles ? 'IN' : 'OUT'}`;
    landingTag = new CSS2DObject(el);
    landingTag.position.set(x, 0.55, z);
    scene.add(landingTag);
  }

  const ui = {
    result: $('result'),
    rX: $('rX'), rZ: $('rZ'), rD: $('rD'), rS: $('rS'),
  };
  function showResult(x, z, res) {
    ui.result.hidden = false;
    ui.rX.textContent = `${x.toFixed(2)} m`;
    ui.rZ.textContent = `${z.toFixed(2)} m`;
    ui.rD.textContent = res.doubles ? 'IN' : 'OUT';
    ui.rD.className = res.doubles ? 'in' : 'out';
    ui.rS.textContent = res.singles ? 'IN' : 'OUT';
    ui.rS.className = res.singles ? 'in' : 'out';
    onStatus(`● Shuttle landed at (${x.toFixed(2)}, ${z.toFixed(2)}) — ${res.doubles ? 'IN' : 'OUT'}`);
  }

  let drop = null;

  function dropAt(x, z) {
    replay = null;
    const landing = new THREE.Vector3(x, LAND_Y, z);
    // Drifts a little away from the net while it falls, then bounces on the same line.
    const dir = new THREE.Vector3(0, 0, z >= 0 ? 1 : -1);
    const start = landing.clone().addScaledVector(dir, -DROP_BACK).setY(LAND_Y + DROP_START_Y);
    const b1 = landing.clone().addScaledVector(dir, 0.2);
    const b2 = b1.clone().addScaledVector(dir, 0.08);
    // Resting pose: lying on its side, skirt trailing back along the flight path.
    const restAxis = new THREE.Vector3(0, Math.cos(1.2), 0).addScaledVector(dir, -Math.sin(1.2)).normalize();
    drop = {
      t0: performance.now(), start, landing, b1, b2, dir,
      restQ: new THREE.Quaternion().setFromUnitVectors(UP, restAxis),
      landQ: null, landed: false, res: judge(x, z),
    };
    shuttle.visible = true;
    onStatus('● Shuttle dropping…');
  }

  function fallPos(u, out) {
    const p = 1 - (1 - u) * (1 - u);                  // horizontal speed bleeds off (drag)
    out.lerpVectors(drop.start, drop.landing, p);
    out.y = LAND_Y + (drop.start.y - LAND_Y) * (1 - u * u);  // accelerates downward
    return out;
  }

  const _a = new THREE.Vector3(), _b = new THREE.Vector3(), _axis = new THREE.Vector3(), _q = new THREE.Quaternion();
  function placeShuttle(pos, quat) {
    shuttle.quaternion.copy(quat);
    // Lift so the tilted cork never sinks into the floor.
    const cos = _axis.copy(UP).applyQuaternion(quat).y;
    shuttle.position.copy(pos);
    shuttle.position.y += CORK_R * (1 - cos);
  }

  function updateDrop(now) {
    if (!drop) return;
    const t = (now - drop.t0) / 1000;
    if (t < FALL_T) {
      const u = t / FALL_T;
      fallPos(u, _a);
      fallPos(Math.min(u + 0.01, 1), _b);
      // Cork leads: the skirt axis points opposite to the velocity.
      _b.subVectors(_a, _b).normalize();
      _q.setFromUnitVectors(UP, _b);
      placeShuttle(_a, _q);
      return;
    }
    if (!drop.landed) {
      drop.landed = true;
      drop.landQ = shuttle.quaternion.clone();
      const { x, z } = drop.landing;
      const color = drop.res.doubles ? IN_COLOR : OUT_COLOR;
      addMarker(x, z, drop.res.doubles);
      addRipple(x, z, color, 0);
      addRipple(x, z, color, 180);
      setTag(x, z, drop.res);
      showResult(x, z, drop.res);
    }
    const tb = t - FALL_T;
    const total = B1_T + B2_T + SETTLE_T;
    const k = Math.min(tb / total, 1);
    _q.slerpQuaternions(drop.landQ, drop.restQ, 1 - (1 - k) * (1 - k));
    if (tb < B1_T) {
      const u = tb / B1_T;
      _a.lerpVectors(drop.landing, drop.b1, u);
      _a.y = LAND_Y + B1_H * 4 * u * (1 - u);
    } else if (tb < B1_T + B2_T) {
      const u = (tb - B1_T) / B2_T;
      _a.lerpVectors(drop.b1, drop.b2, u);
      _a.y = LAND_Y + B2_H * 4 * u * (1 - u);
    } else {
      _a.copy(drop.b2);
    }
    placeShuttle(_a, _q);
    if (k >= 1) drop = null;   // at rest; leave it lying on the court
  }

  // --- Replay of a recorded fall (shuttle_review_app.py "Review") ---------
  // `fall.trajectory` = [{t, phase, x, y, z}] in court metres, phase
  // flight -> contact -> bounce / floor, as saved in reviews/*/fall_###.json.
  const trailGeo = new THREE.BufferGeometry();
  const trail = new THREE.Line(trailGeo, new THREE.LineBasicMaterial({ color: 0xff9f40 }));
  const dripGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
  const drip = new THREE.Line(dripGeo, new THREE.LineDashedMaterial({ color: 0xff9f40, dashSize: 0.05, gapSize: 0.04 }));
  trail.visible = drip.visible = false;
  scene.add(trail, drip);
  const replayBtn = $('replayBtn');
  let replay = null, lastFall = null;

  function playFall(fall) {
    const pts = (fall.trajectory || []).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.t));
    if (pts.length < 2) return false;
    let k = pts.findIndex(p => p.phase === 'contact');
    if (k < 0) k = pts.length - 1;
    const air = p => p.phase === 'flight' || p.phase === 'bounce';
    const pos = pts.map(p => new THREE.Vector3(p.x, air(p) ? Math.max(p.y, LAND_Y) : LAND_Y, p.z));
    trailGeo.setFromPoints(pos);
    trailGeo.setDrawRange(0, 0);
    trail.visible = drip.visible = true;
    // Resting pose: lying on its side, skirt pointing back the way it came.
    const back = pos[Math.max(0, k - 1)].clone().sub(pos[k]).setY(0);
    if (back.lengthSq() < 1e-8) back.set(0, 0, pos[k].z >= 0 ? -1 : 1);
    back.normalize();
    const restAxis = new THREE.Vector3(0, Math.cos(1.2), 0).addScaledVector(back, Math.sin(1.2)).normalize();
    drop = null;
    lastFall = fall;
    replay = {
      pts, pos, k, t0: performance.now(), slow: fall.slowmo || 0.5, res: judge(pts[k].x, pts[k].z),
      q: new THREE.Quaternion().setFromUnitVectors(UP, restAxis), landQ: null,
      restQ: new THREE.Quaternion().setFromUnitVectors(UP, restAxis), name: fall.id ? `fall #${fall.id}` : 'live track',
    };
    shuttle.visible = true;
    replayBtn.hidden = false;
    onStatus(`● Replaying ${replay.name} from the video…`);
    onReplay(fall, pos[k].clone());
    return true;
  }

  function updateReplay(now) {
    if (!replay) return;
    const r = replay, pts = r.pts, last = pts.length - 1;
    const tt = pts[0].t + (now - r.t0) / 1000 * r.slow;    // time in the video
    let i = 0;
    while (i < last - 1 && pts[i + 1].t <= tt) i++;
    const u = THREE.MathUtils.clamp((tt - pts[i].t) / Math.max(1e-6, pts[i + 1].t - pts[i].t), 0, 1);
    _a.lerpVectors(r.pos[i], r.pos[i + 1], u);
    if (i < r.k) {
      // Cork leads: the skirt axis points opposite to the motion.
      _b.subVectors(r.pos[i], r.pos[i + 1]);
      if (_b.lengthSq() > 1e-10) r.q.setFromUnitVectors(UP, _b.normalize());
    } else {
      if (!r.landQ) r.landQ = r.q.clone();
      const s = THREE.MathUtils.clamp((tt - pts[r.k].t) / 0.4, 0, 1);
      r.q.slerpQuaternions(r.landQ, r.restQ, 1 - (1 - s) * (1 - s));
    }
    placeShuttle(_a, r.q);
    trailGeo.setDrawRange(0, i + 1 + (u > 0 ? 1 : 0));
    dripGeo.attributes.position.setXYZ(0, _a.x, _a.y, _a.z);
    dripGeo.attributes.position.setXYZ(1, _a.x, LAND_Y, _a.z);
    dripGeo.attributes.position.needsUpdate = true;
    drip.computeLineDistances();
    if (tt >= pts[r.k].t && !r.landed) {
      r.landed = true;
      const { x, z } = pts[r.k];
      const color = r.res.doubles ? IN_COLOR : OUT_COLOR;
      addMarker(x, z, r.res.doubles);
      addRipple(x, z, color, 0);
      addRipple(x, z, color, 180);
      setTag(x, z, r.res);
      showResult(x, z, r.res);
    }
    if (tt >= pts[last].t) {
      replay = null;           // leave it lying where the video shows it at rest
      trailGeo.setDrawRange(0, pts.length);
      drip.visible = false;
      const { x, z } = pts[r.k];
      onStatus(`● ${r.name}: landed at (${x.toFixed(2)}, ${z.toFixed(2)}) — ${r.res.doubles ? 'IN' : 'OUT'}`);
    }
  }
  replayBtn.addEventListener('click', () => { if (lastFall) playFall(lastFall); });

  function updateRipples(now) {
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i];
      const u = (now - r.born) / 900;
      if (u < 0) continue;
      if (u >= 1) {
        scene.remove(r.mesh); r.mesh.geometry.dispose(); r.mesh.material.dispose();
        ripples.splice(i, 1);
        continue;
      }
      const s = 0.08 + 0.62 * (1 - (1 - u) ** 3);
      r.mesh.scale.set(s, s, 1);
      r.mesh.material.opacity = 0.9 * (1 - u);
    }
  }

  $('clear').addEventListener('click', () => {
    for (const m of markers) {
      scene.remove(m);
      m.traverse(o => { if (o.isMesh) { o.geometry.dispose(); o.material.dispose(); } });
    }
    markers.length = 0;
    if (landingTag) { scene.remove(landingTag); landingTag.element.remove(); landingTag = null; }
    drop = null;
    replay = null;
    shuttle.visible = trail.visible = drip.visible = false;
    ui.result.hidden = true;
    onStatus(null);
  });

  // --- Mode switching -------------------------------------------------
  let mode = 'point';
  const modeBtns = { point: $('modePoint'), coord: $('modeCoord') };
  const pointHint = $('pointHint');
  const coordForm = $('coordForm');
  function setMode(m) {
    mode = m;
    for (const [k, b] of Object.entries(modeBtns)) {
      b.classList.toggle('active', k === m);
      b.setAttribute('aria-pressed', String(k === m));
    }
    pointHint.hidden = m !== 'point';
    coordForm.hidden = m !== 'coord';
    hoverRing.visible = false;
    renderer.domElement.style.cursor = '';
    if (m === 'coord') $('inX').focus();
  }
  modeBtns.point.addEventListener('click', () => setMode('point'));
  modeBtns.coord.addEventListener('click', () => setMode('coord'));

  // --- Point mode: click on the court ----------------------------------
  const raycaster = new THREE.Raycaster();
  const floorPlane = new THREE.Plane(UP, -LAND_Y);
  const ndc = new THREE.Vector2();
  function pickCourt(ev) {
    const rect = renderer.domElement.getBoundingClientRect();
    ndc.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hit = raycaster.ray.intersectPlane(floorPlane, new THREE.Vector3());
    if (!hit || Math.abs(hit.x) > REGION_X || Math.abs(hit.z) > REGION_Z) return null;
    return hit;
  }

  let downAt = null;
  renderer.domElement.addEventListener('pointerdown', ev => {
    downAt = ev.button === 0 ? { x: ev.clientX, y: ev.clientY, t: performance.now() } : null;
  });
  renderer.domElement.addEventListener('pointerup', ev => {
    if (mode !== 'point' || !downAt || ev.button !== 0) return;
    // Treat as a click only if the pointer barely moved (otherwise it was an orbit drag).
    const moved = Math.hypot(ev.clientX - downAt.x, ev.clientY - downAt.y);
    const quick = performance.now() - downAt.t < 600;
    downAt = null;
    if (moved > 6 || !quick) return;
    const hit = pickCourt(ev);
    if (hit) dropAt(hit.x, hit.z);
  });
  renderer.domElement.addEventListener('pointermove', ev => {
    if (mode !== 'point' || ev.buttons) { hoverRing.visible = false; return; }
    const hit = pickCourt(ev);
    hoverRing.visible = !!hit;
    renderer.domElement.style.cursor = hit ? 'crosshair' : '';
    if (hit) hoverRing.position.set(hit.x, LAND_Y + 0.003, hit.z);
  });
  renderer.domElement.addEventListener('pointerleave', () => { hoverRing.visible = false; });

  // --- Co-ordinates mode -----------------------------------------------
  const coordErr = $('coordErr');
  coordForm.addEventListener('submit', ev => {
    ev.preventDefault();
    const x = parseFloat($('inX').value);
    const z = parseFloat($('inZ').value);
    if (!Number.isFinite(x) || !Number.isFinite(z)) {
      coordErr.textContent = 'Enter numeric X and Z values.';
      return;
    }
    if (Math.abs(x) > REGION_X || Math.abs(z) > REGION_Z) {
      coordErr.textContent = `Out of range. Max |X| ${REGION_X.toFixed(2)}, |Z| ${REGION_Z.toFixed(2)}.`;
      return;
    }
    coordErr.textContent = '';
    dropAt(x, z);
  });

  return {
    dropAt,
    playFall,
    update(now) { updateDrop(now); updateReplay(now); updateRipples(now); },
  };
}

// Plays falls from shuttle_review_app.py (it serves this folder):
//   ?review=live              polls reviews/latest_review.json, replays on every Review press
//   ?review=reviews/…/fall_001.json   plays one saved fall
export function watchReviews(shuttleDrop) {
  const src = new URLSearchParams(location.search).get('review');
  if (!src) return;
  const live = src === 'live';
  const url = live ? 'reviews/latest_review.json' : src;
  let seen = null;
  const poll = async () => {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (res.ok) {
        const fall = await res.json();
        const key = live ? fall.seq : url;
        if (key !== seen) {
          seen = key;
          shuttleDrop.playFall(fall);
        }
      }
    } catch (e) { /* app not reachable yet - try again */ }
    if (live) setTimeout(poll, 700);
  };
  poll();
}
