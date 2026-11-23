const norm3 = (v) => { const n = Math.hypot(...v) || 1; return v.map((x) => x / n); };
let imgRenderer, maskRenderer;
function renderSpec(spec, opts) {
  const { scene, camera } = buildFromSpec(THREE, spec, opts);
  const w = spec.image.width, h = spec.image.height;
  if (!imgRenderer) {
    imgRenderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    imgRenderer.setPixelRatio(1);
  }
  imgRenderer.setSize(w, h, false);
  imgRenderer.shadowMap.enabled = !!opts.shadows;
  imgRenderer.shadowMap.type = THREE.PCFSoftShadowMap;
  imgRenderer.render(scene, camera);
  let canvas = imgRenderer.domElement;
  if (opts.imageNoise) {
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const g = c.getContext('2d'); g.drawImage(canvas, 0, 0);
    const d = g.getImageData(0, 0, w, h); const rng = mulberry32((spec.seed ^ 0x9e3779b9) >>> 0);
    for (let i = 0; i < d.data.length; i += 4) {
      const n = (rng() - 0.5) * opts.imageNoise;
      d.data[i] += n; d.data[i + 1] += n; d.data[i + 2] += n;
    }
    g.putImageData(d, 0, 0); canvas = c;
  }
  return canvas.toDataURL('image/png');
}
function renderMask(raw, aspect, w = 160, h = 120) {
  if (!maskRenderer) {
    maskRenderer = new THREE.WebGLRenderer({ antialias: false, preserveDrawingBuffer: true });
    maskRenderer.setPixelRatio(1);
  }
  maskRenderer.setSize(w, h, false);
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0x000000);
  const white = new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide });
  for (const m of raw.meshes) {
    if (m.role !== 'object') continue;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(m.positions, 3));
    if (m.index) g.setIndex(m.index);
    const mesh = new THREE.Mesh(g, white);
    mesh.matrixAutoUpdate = false; mesh.matrix.fromArray(m.matrixWorld); scene.add(mesh);
  }
  const cam = new THREE.PerspectiveCamera(raw.camera.fov, aspect, Math.max(raw.camera.near, 0.001), raw.camera.far);
  cam.matrixAutoUpdate = false; cam.matrix.fromArray(raw.camera.matrixWorld); cam.updateMatrixWorld(true);
  maskRenderer.render(scene, cam);
  const gl = maskRenderer.getContext(); const px = new Uint8Array(w * h * 4);
  gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
  let bits = '';
  for (let i = 0; i < w * h; i++) bits += px[i * 4] > 127 ? '1' : '0';
  return bits;
}
function analyze(raw, aspect) {
  const e = raw.camera.matrixWorld;
  const camera = {
    isPerspective: raw.camera.isPerspective, fov: raw.camera.fov,
    position: [e[12], e[13], e[14]], right: norm3([e[0], e[1], e[2]]),
    up: norm3([e[4], e[5], e[6]]), forward: norm3([-e[8], -e[9], -e[10]]),
  };
  let groundY = null; const objects = []; const roles = {};
  for (const m of raw.meshes) {
    roles[m.role] = (roles[m.role] || 0) + 1;
    const mw = new THREE.Matrix4().fromArray(m.matrixWorld);
    const local = new THREE.Box3(); const v = new THREE.Vector3();
    for (let i = 0; i < m.positions.length; i += 3) local.expandByPoint(v.set(m.positions[i], m.positions[i + 1], m.positions[i + 2]));
    if (local.isEmpty()) continue;
    if (m.role === 'ground') {
      const top = local.clone().applyMatrix4(mw).max.y;
      groundY = groundY === null ? top : Math.max(groundY, top);
    } else if (m.role === 'object') {
      const center = local.getCenter(new THREE.Vector3()).applyMatrix4(mw);
      const scale = new THREE.Vector3(); mw.decompose(new THREE.Vector3(), new THREE.Quaternion(), scale);
      const s = local.getSize(new THREE.Vector3()).multiply(scale);
      objects.push({ center: center.toArray(), size: [Math.abs(s.x), Math.abs(s.y), Math.abs(s.z)], color: m.color });
    }
  }
  return { camera, groundY, objects, roles, mask: renderMask(raw, aspect) };
}
window.lab = {
  renderSpec,
  analyze,
  analyzeSpec: (spec) => {
    const { scene, camera } = buildFromSpec(THREE, spec, {});
    return analyze(previzExtract(scene, camera), spec.image.width / spec.image.height);
  },
};
