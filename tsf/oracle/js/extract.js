function previzExtract(scene, camera) {
  scene.updateMatrixWorld(true); camera.updateMatrixWorld(true);
  const meshes = [];
  scene.traverse((o) => {
    if (!o.isMesh || !o.userData || !o.userData.role) return;
    const pos = o.geometry && o.geometry.attributes && o.geometry.attributes.position;
    if (!pos) return;
    const positions = [];
    for (let i = 0; i < pos.count; i++) positions.push(pos.getX(i), pos.getY(i), pos.getZ(i));
    const mat = Array.isArray(o.material) ? o.material[0] : o.material;
    meshes.push({
      role: String(o.userData.role), positions,
      index: o.geometry.index ? Array.from(o.geometry.index.array) : null,
      matrixWorld: Array.from(o.matrixWorld.elements),
      color: mat && mat.color ? '#' + mat.color.getHexString() : null,
    });
  });
  return {
    camera: { isPerspective: !!camera.isPerspectiveCamera, fov: camera.fov, near: camera.near,
              far: camera.far, matrixWorld: Array.from(camera.matrixWorld.elements) },
    meshes,
  };
}
