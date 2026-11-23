function mulberry32(a) {
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function buildFromSpec(THREE, spec, opts = {}) {
  const rng = mulberry32(spec.seed >>> 0);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(spec.background);
  const { width: W, depth: D, height: H } = spec.room;
  const noiseTexture = () => {
    if (!opts.textureNoise) return null;
    const c = document.createElement('canvas'); c.width = c.height = 128;
    const g = c.getContext('2d'); const img = g.createImageData(128, 128);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = 190 + Math.floor(rng() * 65);
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v; img.data[i + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    const t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(4, 4); t.colorSpace = THREE.SRGBColorSpace;
    return t;
  };
  const surface = (color) => new THREE.MeshStandardMaterial({ color, roughness: 0.95, metalness: 0, map: noiseTexture() });
  const add = (mesh, role) => {
    mesh.userData.role = role; mesh.castShadow = role === 'object'; mesh.receiveShadow = true;
    scene.add(mesh); return mesh;
  };
  const ground = add(new THREE.Mesh(new THREE.PlaneGeometry(W, D), surface(spec.room.floor_color)), 'ground');
  ground.rotation.x = -Math.PI / 2;
  const back = add(new THREE.Mesh(new THREE.PlaneGeometry(W, H), surface(spec.room.wall_color)), 'wall');
  back.position.set(0, H / 2, -D / 2);
  const left = add(new THREE.Mesh(new THREE.PlaneGeometry(D, H), surface(spec.room.wall_color)), 'wall');
  left.position.set(-W / 2, H / 2, 0); left.rotation.y = Math.PI / 2;
  const right = add(new THREE.Mesh(new THREE.PlaneGeometry(D, H), surface(spec.room.wall_color)), 'wall');
  right.position.set(W / 2, H / 2, 0); right.rotation.y = -Math.PI / 2;
  for (const o of spec.objects) {
    const geo = o.shape === 'box'
      ? new THREE.BoxGeometry(o.size[0], o.size[1], o.size[2])
      : new THREE.CylinderGeometry(o.size[0] / 2, o.size[0] / 2, o.size[1], 32);
    const m = add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: o.color, roughness: 0.7 })), 'object');
    m.position.set(o.position[0], o.size[1] / 2, o.position[1]);
    m.rotation.y = o.yaw_deg * Math.PI / 180;
  }
  scene.add(new THREE.AmbientLight(0xffffff, spec.light.ambient));
  const sun = new THREE.DirectionalLight(0xffffff, spec.light.intensity);
  sun.position.set(...spec.light.direction.map((v) => v * 10));
  if (opts.shadows) {
    sun.castShadow = true; sun.shadow.mapSize.set(1024, 1024);
    const s = sun.shadow.camera; s.left = -W; s.right = W; s.top = D; s.bottom = -D; s.far = 40;
  }
  scene.add(sun);
  const c = spec.camera;
  const camera = new THREE.PerspectiveCamera(c.fov, spec.image.width / spec.image.height, 0.05, 100);
  camera.position.set(...c.position);
  camera.lookAt(new THREE.Vector3(...c.target));
  camera.rotateZ(c.roll_deg * Math.PI / 180);
  return { scene, camera };
}
