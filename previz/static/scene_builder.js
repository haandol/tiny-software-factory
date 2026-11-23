export function build(THREE) {
  const S = SPEC;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(S.background);
  const add = (parent, mesh, role, name) => {
    mesh.userData.role = role; mesh.name = name;
    mesh.castShadow = role === 'object'; mesh.receiveShadow = true;
    parent.add(mesh); return mesh;
  };
  const surface = (color) => new THREE.MeshStandardMaterial({ color, roughness: 0.95, metalness: 0 });

  // 방: 카메라 기준으로 놓고 방향(yaw)만큼 돌린다. 벽 법선은 방 안쪽을 본다.
  const r = S.room;
  const room = new THREE.Group(); room.name = 'room'; room.rotation.y = r.yaw; scene.add(room);
  const W = r.right - r.left, D = r.front - r.back, cx = (r.left + r.right) / 2, cz = (r.front + r.back) / 2;
  const floor = add(room, new THREE.Mesh(new THREE.PlaneGeometry(W, D), surface(r.floor_color)), 'ground', 'floor');
  floor.rotation.x = -Math.PI / 2; floor.position.set(cx, 0, cz);
  const wc = (side) => (r.wall_colors && r.wall_colors[side]) || r.wall_color;
  const back = add(room, new THREE.Mesh(new THREE.PlaneGeometry(W, r.height), surface(wc('back'))), 'wall', 'wall-back');
  back.position.set(cx, r.height / 2, r.back);
  const left = add(room, new THREE.Mesh(new THREE.PlaneGeometry(D, r.height), surface(wc('left'))), 'wall', 'wall-left');
  left.position.set(r.left, r.height / 2, cz); left.rotation.y = Math.PI / 2;
  const right = add(room, new THREE.Mesh(new THREE.PlaneGeometry(D, r.height), surface(wc('right'))), 'wall', 'wall-right');
  right.position.set(r.right, r.height / 2, cz); right.rotation.y = -Math.PI / 2;

  // 주요 물체: 물체 하나당 메시 하나
  S.objects.forEach((o, i) => {
    const [w, h, d] = o.size;
    const geo = o.shape === 'cylinder'
      ? new THREE.CylinderGeometry(w / 2, w / 2, h, 48)
      : new THREE.BoxGeometry(w, h, d);
    const m = add(scene, new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: o.color, roughness: 0.7 })),
                  'object', `object-${i + 1}-${o.shape}`);
    m.position.set(o.position[0], h / 2, o.position[1]);
    m.rotation.y = o.yaw || 0;
  });

  // 조명: 주광(방향광, 그림자) + 환경광
  scene.add(new THREE.AmbientLight(0xffffff, S.light.ambient));
  const sun = new THREE.DirectionalLight(0xffffff, S.light.intensity);
  const c = new THREE.Vector3(cx, 0, cz).applyAxisAngle(new THREE.Vector3(0, 1, 0), r.yaw);
  sun.position.copy(c).add(new THREE.Vector3(...S.light.direction).multiplyScalar(30));
  sun.target.position.copy(c);
  sun.castShadow = true; sun.shadow.mapSize.set(1024, 1024);
  const ext = Math.max(W, D, r.height) * 0.9;
  Object.assign(sun.shadow.camera, { left: -ext, right: ext, top: ext, bottom: -ext, near: 1, far: 80 });
  sun.shadow.bias = -0.0005;
  scene.add(sun, sun.target);

  // 카메라: 원본 사진과 같은 시점 (lookAt 다음 롤)
  const k = S.camera;
  const camera = new THREE.PerspectiveCamera(k.fov, k.aspect, 0.05, 500);
  camera.position.set(...k.position);
  camera.lookAt(new THREE.Vector3(...k.target));
  camera.rotateZ(k.roll_deg * Math.PI / 180);
  camera.updateProjectionMatrix(); camera.updateMatrixWorld(true);
  return { scene, camera };
}
