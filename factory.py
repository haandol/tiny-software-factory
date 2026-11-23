#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["playwright==1.65.2", "pyyaml>=6"]
# ///
"""tiny-software-factory — 가장 작은 소프트웨어 팩토리.

이 파일 하나가 팩토리 전체다. 단계마다 명령이 하나씩 늘어난다.

  0단계  init, doctor, linear-setup   로컬 준비, 환경 점검, Linear 준비
  1단계  eval, calibrate              정답 장면 평가기와 채점기 검증
  2단계  run                          로컬 작업 명세 하나를 끝까지 실행
  3단계  up                           (예정) Linear를 제어판으로 상주 실행

실행: uv run factory.py <명령>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import itertools
import json
import math
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# 경로와 상수
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
VAR = ROOT / "var"                       # 실행 중에 생기는 모든 것 (git 제외)
CODEX_HOME = VAR / "codex-home"          # 제품이 Codex를 부를 때 쓰는 전용 설정
PRODUCT = ROOT / "product"               # 제품 저장소 (별도 git, 팩토리만 커밋)
VENDOR = ROOT / "vendor"                 # 합성 장면 렌더링용 three.js 사본

THREE_VERSION = "0.187.0"
THREE_FILES = ["build/three.module.js", "build/three.core.js", "LICENSE"]

LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_LABELS = {                        # 이름: (색, 설명)
    "factory": ("#5e6ad2", "팩토리가 제품 저장소에서 처리하는 이슈"),
    "factory-eval": ("#f2c94c", "팩토리가 평가 자산(oracle.py, CONTRACT.md)에서 처리하는 이슈"),
    "auto-discovered": ("#26b5ce", "발견 루프가 만든 이슈"),
}
NEEDS_HUMAN = "Needs Human"

# 팩토리가 만드는 커밋의 작성자
FACTORY_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "tiny-software-factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "tiny-software-factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}


# ---------------------------------------------------------------------------
# 환경: .env와 Bedrock default profile
# ---------------------------------------------------------------------------

def load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE 형식의 .env를 읽어 환경 변수에 넣는다. 이미 있는 값은 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def aws_default_region() -> str:
    out = subprocess.run(
        ["aws", "configure", "get", "region", "--profile", "default"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() or "us-east-1"


def agent_env(**extra: str) -> dict[str, str]:
    """에이전트 CLI에 넘길 환경. 셸의 AWS 설정과 무관하게 Bedrock default profile을 쓴다."""
    region = aws_default_region()
    env = os.environ.copy()
    env.update(
        AWS_PROFILE="default",
        AWS_REGION=region,
        AWS_DEFAULT_REGION=region,
        CLAUDE_CODE_USE_BEDROCK="1",
    )
    env.pop("ANTHROPIC_MODEL", None)      # 모델은 지정하지 않는다
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Linear: 팩토리의 제어판
# ---------------------------------------------------------------------------

def linear(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY가 없다 (.env 확인)")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        LINEAR_URL, data=body,
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(f"Linear 오류: {data['errors']}")
    return data["data"]


def linear_team(team_key: str) -> dict:
    data = linear(
        """query($key: String!) {
          teams(filter: { key: { eq: $key } }) { nodes {
            id key name
            states { nodes { id name type } }
            labels { nodes { id name } }
          } }
        }""",
        {"key": team_key},
    )
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"Linear 팀 {team_key}를 찾을 수 없다")
    return nodes[0]


def team_key() -> str:
    return os.environ.get("FACTORY_LINEAR_TEAM", "SWF")


# ---------------------------------------------------------------------------
# init: 로컬 준비 (여러 번 실행해도 같은 결과)
# ---------------------------------------------------------------------------

def write_toml(data: dict, prefix: str = "") -> str:
    """config.toml에 필요한 만큼만 지원하는 작은 TOML 작성기 (문자열, 숫자, 불리언, 표)."""
    scalars, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif isinstance(v, bool):
            scalars.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            scalars.append(f"{k} = {v}")
        else:
            scalars.append(f"{k} = {json.dumps(str(v), ensure_ascii=False)}")
    out = ""
    if scalars:
        if prefix:
            out += f"[{prefix}]\n"
        out += "\n".join(scalars) + "\n\n"
    for k, v in tables:
        out += write_toml(v, f"{prefix}.{k}" if prefix else k)
    return out


def init_codex_home() -> Path:
    """사용자 Codex 설정에서 Bedrock 공급자와 모델만 옮긴 전용 설정을 만든다.

    MCP 서버, 플러그인, hook, 전역 AGENTS.md는 옮기지 않는다. 제품 동작이
    개인 설정에 따라 달라지지 않게 하기 위해서다.
    """
    user_config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
    keep = {k: user_config[k] for k in
            ("model_provider", "model", "model_catalog_json", "model_context_window")
            if k in user_config}
    providers = {
        name: conf for name, conf in user_config.get("model_providers", {}).items()
        if name.startswith("amazon-bedrock")
    }
    for conf in providers.values():               # 어떤 공급자든 default profile을 쓴다
        if isinstance(conf.get("aws"), dict):
            conf["aws"]["profile"] = "default"
    if providers:
        keep["model_providers"] = providers
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    header = "# factory.py init이 만든 제품 전용 Codex 설정. 직접 고치지 않는다.\n\n"
    (CODEX_HOME / "config.toml").write_text(header + write_toml(keep))
    return CODEX_HOME / "config.toml"


def init_vendor_three() -> None:
    if all((VENDOR / Path(f).name).exists() for f in THREE_FILES):
        return
    url = f"https://registry.npmjs.org/three/-/three-{THREE_VERSION}.tgz"
    with urllib.request.urlopen(url, timeout=120) as resp:
        archive = tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz")
    VENDOR.mkdir(exist_ok=True)
    for name in THREE_FILES:
        member = archive.getmember(f"package/{name}")
        (VENDOR / Path(name).name).write_bytes(archive.extractfile(member).read())
    (VENDOR / "VERSION").write_text(f"three {THREE_VERSION}\n")


def init_product_repo() -> None:
    if (PRODUCT / ".git").exists():
        return
    PRODUCT.mkdir(exist_ok=True)
    env = {**os.environ, **FACTORY_GIT_IDENTITY}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=PRODUCT, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init: empty product repository"],
        cwd=PRODUCT, env=env, check=True,
    )


def init_browser() -> None:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def cmd_init(args: argparse.Namespace) -> int:
    for d in (VAR, VAR / "runs", VAR / "worktrees"):
        d.mkdir(parents=True, exist_ok=True)
    print(f"codex-home   {init_codex_home().relative_to(ROOT)}")
    init_vendor_three()
    print(f"three.js     vendor/ ({THREE_VERSION})")
    init_product_repo()
    print(f"product      {PRODUCT.relative_to(ROOT)}/ (git main)")
    init_browser()
    print("chromium     playwright 브라우저 설치 완료")
    return 0


# ===========================================================================
# 1단계: 평가 (Oracle)
#
# 정답을 아는 3D 장면을 먼저 만들고, 그 장면을 렌더링한 이미지를 제품에 넣는다.
# 제품의 뷰어가 실제로 그린 three.js 장면 그래프를 읽어 정답과 비교한다.
# 장면 생성·렌더링·측정은 헤드리스 Chromium 안의 three.js가, 비교·채점은 Python이 한다.
# ===========================================================================

IMAGE_W, IMAGE_H = 640, 480
PUBLIC_SEEDS = [1101, 2202, 3303, 4404]
HOLDOUT_COUNT = 4
REQUEST_TIMEOUT_S = 900                 # 요청 하나가 done이 될 때까지 기다리는 최대 시간
VARIANT_OPTS = {                        # 렌더링 변형. 홀드아웃만 그림자와 노이즈를 섞는다
    "public": {},
    "holdout": {"shadows": True, "textureNoise": True, "imageNoise": 18},
}
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]

# 최종 목표. 이슈 하나의 성공 조건은 이 중 일부를 한 단계씩 끌어올린다.
FINAL_TARGETS = {
    "camera.fov_error_deg": ("<=", 5.0),
    "camera.pitch_error_deg": ("<=", 3.0),
    "camera.roll_error_deg": ("<=", 3.0),
    "objects.f1": (">=", 0.8),
    "objects.position_error": ("<=", 0.10),
    "objects.size_error": ("<=", 0.20),
    "objects.color_delta_e": ("<=", 15.0),
    "reprojection.iou": (">=", 0.7),
    "viewer.console_errors": ("<=", 0),
}
# 장면을 끝내 받지 못했을 때 쓰는 최악값
WORST_METRICS = {
    "camera.fov_error_deg": 90.0, "camera.pitch_error_deg": 90.0, "camera.roll_error_deg": 90.0,
    "objects.f1": 0.0, "objects.position_error": 1.0, "objects.size_error": 1.0,
    "objects.color_delta_e": 100.0, "reprojection.iou": 0.0,
}

PALETTE_OBJECT = ["#d1495b", "#2e86ab", "#f4a259", "#5b8e7d", "#8e6bbf", "#edae49", "#3d5a80", "#e07a5f"]
PALETTE_FLOOR = ["#8d8478", "#a39e93", "#7a7f80", "#9c8f80"]
PALETTE_WALL = ["#d9d4c7", "#c9ccd1", "#e3dccf", "#cfd6cf"]


# --- 장면 생성기: 시드 하나 → 정답 장면 -----------------------------------------

def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def generate_scene(seed: int, variant: str) -> dict:
    """방, 카메라, 물체 1~4개, 조명을 시드로 정한다. 같은 시드는 항상 같은 장면이 된다."""
    rng = random.Random(seed)
    W, D, H = rng.uniform(4, 8), rng.uniform(4, 8), rng.uniform(2.6, 3.4)
    fov = rng.uniform(35, 75)
    pitch = math.radians(rng.uniform(-15, 3))
    yaw = math.radians(rng.uniform(-20, 20))
    roll = rng.uniform(-5, 5) if variant == "holdout" else rng.uniform(-3, 3)
    cam = [rng.uniform(-W * 0.2, W * 0.2), rng.uniform(1.0, 1.8), D / 2 - rng.uniform(0.2, 0.8)]
    forward = [math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch)]
    target = [cam[i] + forward[i] * 5 for i in range(3)]

    right = _norm(_cross(forward, [0, 1, 0]))
    up = _cross(right, forward)
    tan_v = math.tan(math.radians(fov) / 2)
    tan_h = tan_v * IMAGE_W / IMAGE_H

    def visible(p):
        v = _sub(p, cam)
        z = _dot(v, forward)
        if z < 1.0:
            return False
        return abs(_dot(v, right) / (z * tan_h)) < 0.8 and abs(_dot(v, up) / (z * tan_v)) < 0.8

    objects, colors = [], rng.sample(PALETTE_OBJECT, 4)
    for _ in range(rng.randint(1, 4)):
        for _attempt in range(300):
            shape = rng.choice(["box", "cylinder"])
            if shape == "box":
                size = [rng.uniform(0.4, 1.4), rng.uniform(0.3, 1.6), rng.uniform(0.4, 1.4)]
            else:
                dia = rng.uniform(0.4, 1.2)
                size = [dia, rng.uniform(0.4, 1.6), dia]
            radius = max(size[0], size[2]) * 0.75
            x = rng.uniform(-W / 2 + radius, W / 2 - radius)
            z = rng.uniform(-D / 2 + radius, cam[2] - 1.2)
            if not (visible([x, size[1] / 2, z]) and visible([x, size[1], z])):
                continue
            if any(math.dist([x, z], o["position"]) < radius + o["_r"] + 0.1 for o in objects):
                continue
            objects.append({"shape": shape, "size": size, "position": [x, z],
                            "yaw_deg": rng.uniform(0, 90) if shape == "box" else 0.0,
                            "color": colors[len(objects)], "_r": radius})
            break
    for o in objects:
        o.pop("_r")
    if not objects:  # 드물게 배치에 실패하면 다음 시드로 대신한다
        return generate_scene(seed + 7919, variant) | {"seed": seed}

    light_dir = _norm([rng.uniform(-1, 1), rng.uniform(1.5, 3), rng.uniform(0.3, 1.2)])
    lo, hi = (1.2, 3.4) if variant == "holdout" else (1.8, 2.8)
    return {
        "seed": seed, "variant": variant,
        "image": {"width": IMAGE_W, "height": IMAGE_H},
        "background": "#e9eef3",
        "room": {"width": W, "depth": D, "height": H,
                 "floor_color": rng.choice(PALETTE_FLOOR), "wall_color": rng.choice(PALETTE_WALL)},
        "camera": {"fov": fov, "position": cam, "target": target, "roll_deg": roll},
        "objects": objects,
        "light": {"direction": light_dir, "intensity": rng.uniform(lo, hi), "ambient": rng.uniform(0.35, 0.6)},
    }


# --- 브라우저 쪽 코드: 장면 만들기, 장면 그래프 읽기, 측정 ---------------------------

BUILDER_JS = r"""
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
"""

# 뷰어 페이지에 주입해 계약된 장면 그래프를 그대로 읽어 온다 (role이 붙은 메시만).
EXTRACT_JS = r"""
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
"""

# 팩토리 실험실 페이지: 정답 이미지 렌더링, 정답 측정, 제품 장면 측정(실루엣 포함)
LAB_JS = r"""
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
"""

LAB_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<script type="importmap">{{"imports": {{"three": "http://factory.local/three.module.js"}}}}</script>
</head><body><script type="module">
import * as THREE from 'three';
{BUILDER_JS}
{EXTRACT_JS}
{LAB_JS}
</script></body></html>"""


# --- 채점: 정답 측정값과 제품 측정값 비교 ------------------------------------------

def _hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    x = (lin[0] * 0.4124 + lin[1] * 0.3576 + lin[2] * 0.1805) / 0.95047
    y = lin[0] * 0.2126 + lin[1] * 0.7152 + lin[2] * 0.0722
    z = (lin[0] * 0.0193 + lin[1] * 0.1192 + lin[2] * 0.9505) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _delta_e(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 100.0
    return math.dist(_hex_to_lab(a), _hex_to_lab(b))


def _camera_frame(an: dict):
    """카메라 바로 아래 바닥점을 원점으로, 카메라가 보는 수평 방향을 +z로 하고, 카메라 높이로 나눈 좌표계."""
    cam = an["camera"]
    p, f = cam["position"], cam["forward"]
    ground_y = an["groundY"] if an["groundY"] is not None else 0.0
    height = p[1] - ground_y
    if height <= 1e-6:
        return None
    fh = _norm([f[0], 0.0, f[2]])
    xr = _cross(fh, [0.0, 1.0, 0.0])
    origin = [p[0], ground_y, p[2]]

    def to_local(point):
        v = _sub(point, origin)
        return [_dot(v, xr) / height, v[1] / height, _dot(v, fh) / height]
    return {"to_local": to_local, "height": height}


def _angles(an: dict) -> tuple[float, float]:
    cam = an["camera"]
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, cam["forward"][1]))))
    roll = math.degrees(math.asin(max(-1.0, min(1.0, cam["right"][1]))))
    return pitch, roll


def _iou(a: str, b: str) -> float:
    ia, ib = int(a, 2), int(b, 2)
    union = (ia | ib).bit_count()
    return (ia & ib).bit_count() / union if union else 1.0


def score_metrics(m: dict) -> float:
    """CONTRACT.md 6절의 충실도 점수."""
    clamp = lambda x: max(0.0, min(1.0, x))  # noqa: E731
    s_fov = clamp(1 - m["camera.fov_error_deg"] / 30)
    s_pose = clamp(1 - (m["camera.pitch_error_deg"] + m["camera.roll_error_deg"]) / 2 / 15)
    s_pos = clamp(1 - m["objects.position_error"] / 0.5)
    s_sc = (clamp(1 - m["objects.size_error"]) + clamp(1 - m["objects.color_delta_e"] / 60)) / 2
    s_iou = clamp(m["reprojection.iou"])
    weighted = 0.20 * s_fov + 0.15 * s_pose + 0.20 * s_pos + 0.15 * s_sc + 0.30 * s_iou
    return round(100 * m["objects.f1"] * weighted, 2)


def compare(gt: dict, pred: dict, spec: dict) -> dict:
    """정답 측정값(gt)과 제품 측정값(pred)을 지표로 바꾼다."""
    m = dict(WORST_METRICS)
    g_pitch, g_roll = _angles(gt)
    p_pitch, p_roll = _angles(pred)
    m["camera.fov_error_deg"] = abs(pred["camera"]["fov"] - gt["camera"]["fov"])
    m["camera.pitch_error_deg"] = abs(p_pitch - g_pitch)
    m["camera.roll_error_deg"] = abs(p_roll - g_roll)
    m["reprojection.iou"] = _iou(gt["mask"], pred["mask"])

    gf, pf = _camera_frame(gt), _camera_frame(pred)
    if gf and pf:
        g_objs = [{"c": gf["to_local"](o["center"]), "s": [x / gf["height"] for x in o["size"]], "color": o["color"]}
                  for o in gt["objects"]]
        p_objs = [{"c": pf["to_local"](o["center"]), "s": [x / pf["height"] for x in o["size"]], "color": o["color"]}
                  for o in pred["objects"]]
        # 정답 물체마다 가장 가까운 후보 12개 안에서 거리 합이 최소인 짝을 고른다
        if len(p_objs) > 12:
            p_objs = sorted(p_objs, key=lambda p: min(math.dist(p["c"], g["c"]) for g in g_objs))[:12]
        best, best_cost = [], float("inf")
        k = min(len(g_objs), len(p_objs))
        for g_sel in itertools.combinations(range(len(g_objs)), k):
            for p_sel in itertools.permutations(range(len(p_objs)), k):
                pairs = [(gi, pi) for gi, pi in zip(g_sel, p_sel)
                         if math.dist(g_objs[gi]["c"], p_objs[pi]["c"]) < 1.0]
                cost = sum(math.dist(g_objs[gi]["c"], p_objs[pi]["c"]) for gi, pi in pairs) - 10 * len(pairs)
                if cost < best_cost:
                    best, best_cost = pairs, cost
        tp = len(best)
        m["objects.f1"] = 2 * tp / (len(g_objs) + len(p_objs)) if (g_objs or p_objs) else 1.0
        if tp:
            room_diag = math.hypot(spec["room"]["width"], spec["room"]["depth"]) / gf["height"]
            pos_err, size_err, color_err = [], [], []
            for gi, pi in best:
                g, p = g_objs[gi], p_objs[pi]
                pos_err.append(math.hypot(g["c"][0] - p["c"][0], g["c"][2] - p["c"][2]) / room_diag)
                gh, ph = sorted([g["s"][0], g["s"][2]]), sorted([p["s"][0], p["s"][2]])
                rel = [abs(ph[i] - gh[i]) / gh[i] for i in range(2)] + [abs(p["s"][1] - g["s"][1]) / g["s"][1]]
                size_err.append(sum(rel) / 3)
                color_err.append(_delta_e(g["color"], p["color"]))
            m["objects.position_error"] = sum(pos_err) / tp
            m["objects.size_error"] = sum(size_err) / tp
            m["objects.color_delta_e"] = sum(color_err) / tp
    return {k: round(v, 4) for k, v in m.items()}


# --- 평가 대상 앱: 제품 프로세스 또는 채점기 검증용 기준 앱 --------------------------

@dataclass
class AppHandle:
    base_url: str
    data_dir: Path
    stop: Callable[[], None]
    log_path: Path | None = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_product_app(app_dir: Path, data_dir: Path, log_path: Path) -> AppHandle:
    """CONTRACT.md 1절대로 제품을 띄운다."""
    port = _free_port()
    data_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w")
    proc = subprocess.Popen(
        ["uv", "run", "app.py", "--port", str(port), "--data-dir", str(data_dir)],
        cwd=app_dir, env=agent_env(CODEX_HOME=str(CODEX_HOME)),
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )

    def stop():
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()

    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            stop()
            raise RuntimeError(f"app.py가 시작 중에 종료됐다 (exit {proc.returncode}, 로그 {log_path})")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return AppHandle(f"http://127.0.0.1:{port}", data_dir, stop, log_path)
        time.sleep(0.5)
    stop()
    raise RuntimeError(f"app.py가 180초 안에 포트 {port}를 열지 않았다 (로그 {log_path})")


CALIBRATION_VIEWER = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#111}canvas{display:block}</style>
<script type="importmap">{"imports": {"three": "/vendor/three.module.js"}}</script>
</head><body><script type="module">
import * as THREE from 'three';
let resolveReady; window.__PREVIZ__ = { ready: new Promise((r) => (resolveReady = r)) };
const key = new URLSearchParams(location.search).get('key');
const { build } = await import(`/scenes/${key}.js`);
const { scene, camera } = build(THREE);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(innerWidth, innerHeight);
camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
document.body.appendChild(renderer.domElement);
renderer.render(scene, camera);
Object.assign(window.__PREVIZ__, { scene, camera });
resolveReady();
</script></body></html>"""

EMPTY_SPEC = {
    "seed": 0, "image": {"width": IMAGE_W, "height": IMAGE_H}, "background": "#e9eef3",
    "room": {"width": 6, "depth": 6, "height": 3, "floor_color": "#8d8478", "wall_color": "#d9d4c7"},
    "camera": {"fov": 50, "position": [0, 1.6, 2.5], "target": [0, 1.2, -3], "roll_deg": 0},
    "objects": [], "light": {"direction": [0.3, 0.9, 0.3], "intensity": 2.0, "ambient": 0.5},
}


def perturb(spec: dict, level: float) -> dict:
    """정답에 정해진 방향으로 오차를 더한다. level이 클수록 모든 항목이 같은 쪽으로 더 틀린다."""
    s = json.loads(json.dumps(spec))
    s["camera"]["fov"] = min(110.0, s["camera"]["fov"] * (1 + level))
    s["camera"]["target"][1] -= level * 2.5
    s["camera"]["roll_deg"] += level * 10
    for o in s["objects"]:
        o["position"][0] += level * 1.0
        o["size"] = [x * (1 + level) for x in o["size"]]
        rgb = [int(o["color"][i:i + 2], 16) for i in (1, 3, 5)]
        o["color"] = "#" + "".join(f"{round(c + (128 - c) * level):02x}" for c in rgb)
    return s


def calibration_app(kind: str, level: float, registry: dict, data_dir: Path) -> AppHandle:
    """채점기 검증용 기준 앱. 계약만 구현하고 Codex는 부르지 않는다.

    gt: 평가기가 등록한 정답 장면을 그대로 돌려준다 (검증 전용)
    empty: 바닥과 벽만 있는 기본 장면을 돌려준다
    noise: 정답에 level만큼 오차를 더해 돌려준다
    """
    (data_dir / "scenes").mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}

    def resolve(image: bytes) -> dict | None:
        spec = registry.get(hashlib.sha256(image).hexdigest())
        if kind == "empty":
            return EMPTY_SPEC
        if spec is None:
            return None
        return spec if kind == "gt" else perturb(spec, level)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes | str, ctype: str = "application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/api/requests":
                return self._send(404, "{}")
            image = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            key = uuid.uuid4().hex[:12]
            spec = resolve(image)
            if spec is None:
                status[key] = "failed"
            else:
                module = (f"const SPEC = {json.dumps(spec)};\n{BUILDER_JS}\n"
                          "export function build(THREE) { return buildFromSpec(THREE, SPEC, {}); }\n")
                (data_dir / "scenes" / f"{key}.js").write_text(module)
                status[key] = "done"
            self._send(202, json.dumps({"key": key}))

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path.startswith("/api/requests/"):
                key = url.path.rsplit("/", 1)[-1]
                if key not in status:
                    return self._send(404, "{}")
                return self._send(200, json.dumps({"status": status[key]}))
            if url.path == "/view":
                return self._send(200, CALIBRATION_VIEWER, "text/html; charset=utf-8")
            if url.path.startswith("/scenes/") and url.path.endswith(".js"):
                f = data_dir / "scenes" / Path(url.path).name
                if f.exists():
                    return self._send(200, f.read_bytes(), "text/javascript")
            if url.path.startswith("/vendor/"):
                f = VENDOR / Path(url.path).name
                if f.exists():
                    return self._send(200, f.read_bytes(), "text/javascript")
            self._send(404, "{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return AppHandle(f"http://127.0.0.1:{server.server_port}", data_dir,
                     lambda: (server.shutdown(), server.server_close()))


# --- 평가 실행 -------------------------------------------------------------------

@dataclass
class Fixture:
    id: str
    seed: int
    variant: str
    spec: dict = field(default_factory=dict)
    png: bytes = b""
    gt: dict = field(default_factory=dict)


def make_fixtures(suite: str, holdout_seeds: list[int] | None = None) -> list[Fixture]:
    out = []
    if suite in ("public", "all"):
        out += [Fixture(f"public-{i + 1:02d}", s, "public") for i, s in enumerate(PUBLIC_SEEDS)]
    if suite in ("holdout", "all"):
        seeds = holdout_seeds or [random.SystemRandom().randrange(10_000, 1_000_000) for _ in range(HOLDOUT_COUNT)]
        out += [Fixture(f"holdout-{s}", s, "holdout") for s in seeds]
    for fx in out:
        fx.spec = generate_scene(fx.seed, fx.variant)
    return out


def _http(method: str, url: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 30):
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, str(e).encode()


class Lab:
    """헤드리스 Chromium 하나로 정답 이미지 렌더링, 뷰어 측정, 실루엣 비교를 한다."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(args=CHROMIUM_ARGS)
        self.context = self.browser.new_context(viewport={"width": IMAGE_W, "height": IMAGE_H})
        self.external_requests: list[str] = []

        def guard(route):  # 로컬이 아닌 주소로 가는 요청은 막고 기록한다
            host = urllib.parse.urlparse(route.request.url).hostname
            if host in ("127.0.0.1", "localhost"):
                return route.continue_()
            self.external_requests.append(route.request.url)
            return route.abort()

        def serve_factory(route):
            name = urllib.parse.urlparse(route.request.url).path.lstrip("/")
            if name == "lab.html":
                return route.fulfill(body=LAB_HTML, content_type="text/html")
            f = VENDOR / name
            if f.exists():
                return route.fulfill(body=f.read_bytes(), content_type="text/javascript")
            return route.fulfill(status=404, body="")

        self.context.route("**/*", guard)                      # 먼저 등록한 경로가 나중에 검사된다
        self.context.route("http://factory.local/**", serve_factory)
        self.page = self.context.new_page()
        self.page.goto("http://factory.local/lab.html")
        self.page.wait_for_function("() => !!window.lab", timeout=30_000)

    def render(self, fx: Fixture) -> None:
        url = self.page.evaluate("([s, o]) => window.lab.renderSpec(s, o)", [fx.spec, VARIANT_OPTS[fx.variant]])
        fx.png = base64.b64decode(url.split(",", 1)[1])
        fx.gt = self.page.evaluate("(s) => window.lab.analyzeSpec(s)", fx.spec)

    def measure_viewer(self, url: str, screenshot: Path) -> tuple[dict | None, int, str | None]:
        """뷰어를 열어 장면 그래프를 읽는다. (측정값, 콘솔 오류 수, 실패 이유)"""
        page = self.context.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_function("() => !!window.__PREVIZ__", timeout=30_000)
            raw = page.evaluate(
                "async () => {" + EXTRACT_JS + """
                  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('ready 시간 초과 (60초)')), 60000));
                  await Promise.race([window.__PREVIZ__.ready, timeout]);
                  const p = window.__PREVIZ__;
                  if (!p.scene || !p.camera) throw new Error('scene 또는 camera가 없다');
                  return previzExtract(p.scene, p.camera);
                }""")
            page.screenshot(path=str(screenshot))
            analysis = self.page.evaluate("([r, a]) => window.lab.analyze(r, a)", [raw, IMAGE_W / IMAGE_H])
            return analysis, len(errors), None
        except Exception as e:
            return None, len(errors), str(e).splitlines()[0][:300]
        finally:
            page.close()

    def screenshot_html(self, html: str, out: Path, width: int = 1200) -> Path:
        """HTML 한 장을 그려 PNG로 남긴다. 평가 시트와 Linear 댓글 첨부에 쓴다."""
        page = self.context.new_page()
        try:
            page.set_viewport_size({"width": width, "height": 400})
            page.set_content(html, wait_until="load")
            page.screenshot(path=str(out), full_page=True)
        finally:
            page.close()
        return out

    def close(self):
        self.browser.close()
        self._pw.stop()


SHEET_CSS = """
body{margin:0;background:#f4f5f7;font-family:'WenQuanYi Zen Hei',sans-serif;color:#1f2328}
.wrap{padding:20px 24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#57606a;font-size:13px;margin-bottom:14px}
.row{display:flex;gap:12px;align-items:stretch;background:#fff;border:1px solid #d0d7de;border-radius:8px;
     padding:10px;margin-bottom:10px}
.row img{width:320px;height:240px;object-fit:cover;border-radius:4px;background:#ddd}
.cap{font-size:12px;color:#57606a;margin-top:4px}
.meta{font-size:13px;min-width:250px}
.meta b{font-size:15px}
table{border-collapse:collapse;margin-top:6px}
td{padding:2px 8px 2px 0;font-size:12px}
.ok{color:#1a7f37}.bad{color:#cf222e}
.ph{width:320px;height:240px;display:flex;align-items:center;justify-content:center;background:#eaeef2;
    border-radius:4px;color:#8c959f;font-size:13px}
"""


def _img_tag(path: Path, caption: str) -> str:
    if path.exists():
        data = base64.b64encode(path.read_bytes()).decode()
        return f'<div><img src="data:image/png;base64,{data}"><div class="cap">{caption}</div></div>'
    return f'<div><div class="ph">없음</div><div class="cap">{caption}</div></div>'


def eval_sheet_html(summary: dict, eval_dir: Path, title: str) -> str:
    """장면마다 입력 이미지, 뷰어 캡처, 지표를 한 줄로 놓은 평가 시트."""
    smoke = summary["smoke"]
    sub = [f"계약 스모크 {'통과' if smoke['passed'] else '실패'}"]
    for v in ("public", "holdout"):
        if v in summary:
            sub.append(f"{v} 평균 충실도 {summary[v]['fidelity']:.1f}")
    rows = []
    for r in summary["fixtures"]:
        m = r["metrics"]

        def cell(key, fmt, good):
            val = m.get(key)
            if val is None:
                return "<td>-</td>"
            cls = "ok" if good(val) else "bad"
            return f'<td class="{cls}">{fmt.format(val)}</td>'
        t = FINAL_TARGETS
        table = "".join([
            f"<tr><td>화각 오차</td>{cell('camera.fov_error_deg', '{:.1f}°', lambda v: v <= t['camera.fov_error_deg'][1])}</tr>",
            f"<tr><td>피치 / 롤 오차</td>{cell('camera.pitch_error_deg', '{:.1f}°', lambda v: v <= 3)}"
            f"{cell('camera.roll_error_deg', '{:.1f}°', lambda v: v <= 3)}</tr>",
            f"<tr><td>물체 F1</td>{cell('objects.f1', '{:.2f}', lambda v: v >= t['objects.f1'][1])}</tr>",
            f"<tr><td>위치 오차</td>{cell('objects.position_error', '{:.3f}', lambda v: v <= t['objects.position_error'][1])}</tr>",
            f"<tr><td>크기 오차 / ΔE</td>{cell('objects.size_error', '{:.2f}', lambda v: v <= t['objects.size_error'][1])}"
            f"{cell('objects.color_delta_e', '{:.1f}', lambda v: v <= t['objects.color_delta_e'][1])}</tr>",
            f"<tr><td>실루엣 IoU</td>{cell('reprojection.iou', '{:.2f}', lambda v: v >= t['reprojection.iou'][1])}</tr>",
            f"<tr><td>생성 시간</td><td>{m.get('run.seconds', '-')}{'초' if m.get('run.seconds') is not None else ''}</td></tr>",
            f"<tr><td>콘솔 오류</td>{cell('viewer.console_errors', '{:.0f}', lambda v: v == 0)}</tr>",
        ])
        err = f'<div class="cap bad">{r["error"]}</div>' if r.get("error") else ""
        rows.append(
            f'<div class="row">{_img_tag(eval_dir / r["fixture"] / "input.png", "입력 이미지 (정답 장면 렌더링)")}'
            f'{_img_tag(eval_dir / r["fixture"] / "viewer.png", "앱 뷰어 (같은 시점)")}'
            f'<div class="meta"><b>{r["fixture"]}</b> · {r["status"]}<br>충실도 <b>{r["fidelity"]:.1f}</b>'
            f'<table>{table}</table>{err}</div></div>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{SHEET_CSS}</style></head><body>'
            f'<div class="wrap"><h1>{title}</h1><div class="sub">{" · ".join(sub)}</div>{"".join(rows)}</div>'
            "</body></html>")


def run_eval(app: AppHandle, fixtures: list[Fixture], out_dir: Path, lab: Lab,
             registry: dict | None = None) -> dict:
    """fixtures를 app에 요청하고 측정·채점한다. 결과는 out_dir/summary.json에도 남긴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    smoke: dict[str, list[str]] = {}   # 계약 스모크 위반: 항목 → 사례

    def violation(name: str, detail: str):
        smoke.setdefault(name, []).append(detail)

    for fx in fixtures:
        if not fx.png:
            lab.render(fx)
        if registry is not None:
            registry[hashlib.sha256(fx.png).hexdigest()] = fx.spec
        d = out_dir / fx.id
        d.mkdir(exist_ok=True)
        (d / "input.png").write_bytes(fx.png)
        (d / "gt.json").write_text(json.dumps(fx.spec, indent=2))

    # 1) 모두 요청한다
    submitted = {}
    for fx in fixtures:
        code, body = _http("POST", f"{app.base_url}/api/requests", fx.png, "image/png")
        try:
            key = json.loads(body).get("key") if code == 202 else None
        except (ValueError, AttributeError):
            key = None
        if not key:
            violation("post", f"{fx.id}: {code} {body[:120]!r}")
        submitted[fx.id] = {"key": key, "t0": time.time(), "status": "failed" if not key else "pending"}

    # 2) done이 될 때까지 기다린다
    deadline = time.time() + REQUEST_TIMEOUT_S
    while time.time() < deadline and any(s["status"] == "pending" for s in submitted.values()):
        for fx_id, s in submitted.items():
            if s["status"] != "pending":
                continue
            code, body = _http("GET", f"{app.base_url}/api/requests/{s['key']}")
            try:
                status = json.loads(body).get("status") if code == 200 else None
            except (ValueError, AttributeError):
                status = None
            if status not in ("pending", "done", "failed"):
                violation("status", f"{fx_id}: {code} {body[:120]!r}")
                s["status"] = "failed"
            elif status != "pending":
                s["status"], s["seconds"] = status, round(time.time() - s["t0"], 1)
                s["error"] = json.loads(body).get("error")
        time.sleep(0.5 if registry is not None else 2)
    for s in submitted.values():
        if s["status"] == "pending":
            s["status"], s["error"] = "timeout", f"{REQUEST_TIMEOUT_S}초 안에 done이 되지 않았다"

    # 3) 뷰어를 측정하고 채점한다
    results = []
    for fx in fixtures:
        s = submitted[fx.id]
        r = {"fixture": fx.id, "seed": fx.seed, "variant": fx.variant, "key": s["key"],
             "status": s["status"], "seconds": s.get("seconds"), "error": s.get("error"),
             "console_errors": 0, "metrics": dict(WORST_METRICS), "fidelity": 0.0}
        if s["status"] == "done":
            scene_file = app.data_dir / "scenes" / f"{s['key']}.js"
            if not scene_file.exists():
                violation("scene_file", f"{fx.id}: {scene_file.name} 없음")
            elif "build" not in scene_file.read_text(errors="replace"):
                violation("scene_file", f"{fx.id}: build 함수가 보이지 않는다")
            before = len(lab.external_requests)
            pred, n_err, err = lab.measure_viewer(f"{app.base_url}/view?key={s['key']}",
                                                  out_dir / fx.id / "viewer.png")
            if len(lab.external_requests) > before:
                violation("local_only", f"{fx.id}: {lab.external_requests[before]}")
            r["console_errors"] = n_err
            if pred is None:
                violation("viewer", f"{fx.id}: {err}")
                r["error"] = err
            elif not pred["camera"]["isPerspective"]:
                violation("viewer", f"{fx.id}: camera가 PerspectiveCamera가 아니다")
            elif not pred["roles"].get("ground"):
                violation("ground_mesh", f"{fx.id}: role=ground 메시가 없다")
            else:
                r["metrics"] = compare(fx.gt, pred, fx.spec)
                r["fidelity"] = score_metrics(r["metrics"])
                (out_dir / fx.id / "measured.json").write_text(
                    json.dumps({k: v for k, v in pred.items() if k != "mask"}, indent=2))
        r["metrics"]["viewer.console_errors"] = r["console_errors"]
        if s.get("seconds") is not None:
            r["metrics"]["run.seconds"] = s["seconds"]
        (out_dir / fx.id / "result.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
        results.append(r)

    if not any(r["status"] == "done" for r in results):
        violation("done", "done이 된 요청이 하나도 없다")
    summary = summarize(results)
    summary["smoke"] = {"passed": not smoke, "violations": smoke}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    try:  # 사람이 한눈에 볼 수 있는 평가 시트. 실패해도 평가 결과에는 영향이 없다
        lab.screenshot_html(eval_sheet_html(summary, out_dir, out_dir.name), out_dir / "sheet.png")
    except Exception as e:
        print(f"  평가 시트를 만들지 못했다: {e}")
    return summary


def summarize(results: list[dict]) -> dict:
    def mean(key: str, rows: list[dict]):
        vals = [r["metrics"][key] for r in rows if key in r["metrics"]]
        return round(sum(vals) / len(vals), 4) if vals else None

    def block(rows: list[dict]) -> dict:
        keys = sorted({k for r in rows for k in r["metrics"]})
        return {"count": len(rows), "fidelity": round(sum(r["fidelity"] for r in rows) / len(rows), 2) if rows else None,
                "metrics": {k: mean(k, rows) for k in keys}}

    by_variant = {v: block([r for r in results if r["variant"] == v])
                  for v in ("public", "holdout") if any(r["variant"] == v for r in results)}
    return {"overall": block(results), **by_variant, "fixtures": results}


def print_summary(label: str, summary: dict) -> None:
    print(f"\n[{label}] smoke={'통과' if summary['smoke']['passed'] else '실패'}")
    for name, cases in summary["smoke"]["violations"].items():
        print(f"  계약 위반 {name}: {cases[0]}" + (f" 외 {len(cases) - 1}건" if len(cases) > 1 else ""))
    print(f"  {'fixture':<16}{'status':<9}{'fidelity':>9}{'fov°':>8}{'pitch°':>8}{'f1':>6}{'pos':>7}{'iou':>6}")
    for r in summary["fixtures"]:
        m = r["metrics"]
        print(f"  {r['fixture']:<16}{r['status']:<9}{r['fidelity']:>9.1f}{m['camera.fov_error_deg']:>8.1f}"
              f"{m['camera.pitch_error_deg']:>8.1f}{m['objects.f1']:>6.2f}{m['objects.position_error']:>7.3f}"
              f"{m['reprojection.iou']:>6.2f}")
    for v in ("public", "holdout"):
        if v in summary:
            print(f"  평균 {v:<8} fidelity {summary[v]['fidelity']:.1f}")


def new_eval_dir(label: str) -> Path:
    d = VAR / "evals" / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_eval(args: argparse.Namespace) -> int:
    app_dir = Path(args.app).resolve()
    if not (app_dir / "app.py").exists():
        print(f"{app_dir}/app.py가 없다")
        return 1
    out = new_eval_dir(args.label or app_dir.name)
    seeds = [int(s) for s in args.holdout_seeds.split(",")] if args.holdout_seeds else None
    fixtures = make_fixtures(args.suite, seeds)
    app = start_product_app(app_dir, out / "data", out / "app.log")
    lab = Lab()
    try:
        summary = run_eval(app, fixtures, out, lab)
    finally:
        lab.close()
        app.stop()
    print_summary(args.label or app_dir.name, summary)
    print(f"\n결과: {out.relative_to(ROOT)}/summary.json")
    return 0 if summary["smoke"]["passed"] else 1


def cmd_calibrate(args: argparse.Namespace) -> int:
    """기준 앱 세 종류로 채점기가 좋은 결과와 나쁜 결과를 구분하는지 확인한다."""
    out = new_eval_dir("calibrate")
    fixtures = make_fixtures("all")
    registry: dict = {}
    lab = Lab()
    checks: list[tuple[str, bool, str]] = []
    try:
        # 같은 시드를 두 번 렌더링하면 같은 이미지가 나와야 한다
        a, b = Fixture("x", PUBLIC_SEEDS[0], "public"), Fixture("y", PUBLIC_SEEDS[0], "public")
        a.spec = b.spec = generate_scene(PUBLIC_SEEDS[0], "public")
        lab.render(a)
        lab.render(b)
        checks.append(("결정성", a.png == b.png, "같은 시드 두 번 렌더링 → 같은 PNG"))

        runs = [("gt", 0.0), ("empty", 0.0)] + [("noise", lv) for lv in (0.0, 0.1, 0.2, 0.4)]
        scores = {}
        for kind, level in runs:
            label = kind if kind != "noise" else f"noise-{level:.1f}"
            app = calibration_app(kind, level, registry, out / label / "data")
            try:
                summary = run_eval(app, fixtures, out / label, lab, registry)
            finally:
                app.stop()
            print_summary(label, summary)
            scores[label] = summary["overall"]["fidelity"]
            checks.append((f"계약 스모크 ({label})", summary["smoke"]["passed"], "기준 앱이 계약대로 동작"))
    finally:
        lab.close()

    noise = [scores[f"noise-{lv:.1f}"] for lv in (0.0, 0.1, 0.2, 0.4)]
    checks += [
        ("정답 앱", scores["gt"] >= 95, f"{scores['gt']:.1f}점 (기준 95 이상)"),
        ("빈 앱", scores["empty"] <= 5, f"{scores['empty']:.1f}점 (기준 5 이하)"),
        ("오차 주입 앱", noise[0] >= 95 and all(x > y for x, y in zip(noise, noise[1:])),
         " > ".join(f"{x:.1f}" for x in noise) + " (오차 0%·10%·20%·40%, 단조 감소)"),
    ]
    print("\n채점기 검증")
    for name, ok, detail in checks:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:<20} {detail}")
    (out / "calibration.json").write_text(json.dumps(
        {"scores": scores, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]},
        indent=2, ensure_ascii=False))
    print(f"\n결과: {out.relative_to(ROOT)}/calibration.json")
    return 0 if all(ok for _, ok, _ in checks) else 1


# ===========================================================================
# 2단계: 단일 실행기
#
# 작업 명세 하나를 받아 worktree를 만들고, Claude Code에게 구현을 맡기고, 평가한다.
# eval을 통과하면 main에 병합하고 배포한다. 통과하지 못하면 실패 증거를 넘겨 다시 시도하고,
# 시도를 다 쓰면 병합하지 않고 사람에게 넘긴다.
# ===========================================================================

MAX_ATTEMPTS = 3
CLAUDE_BUDGET_PER_ATTEMPT_USD = 5
CLAUDE_TIMEOUT_S = 60 * 60
REGRESSION_TOLERANCE = 2.0              # 홀드아웃 회귀 허용폭 (2단계 실측 후 조정)
PROD_PORT = int(os.environ.get("FACTORY_PROD_PORT", "8000"))
WORKTREES = VAR / "worktrees"
RUNS = VAR / "runs"

# 하네스 v1: 구현 에이전트에게 주는 지시문. 5단계에서 harness/로 옮긴다.
IMPLEMENT_SYSTEM = """\
너는 tiny-software-factory의 구현 에이전트다. 현재 폴더는 제품 저장소의 작업 공간(git worktree)이다.

규칙
- 작업 명세는 .factory/work-item.yaml, 앱 계약은 .factory/CONTRACT.md에 있다. 계약은 반드시 지킨다.
- .factory/feedback.json이 있으면 이전 시도가 eval을 통과하지 못한 것이다. 실패 항목과 증거부터 읽는다.
- .factory/ 아래 파일은 읽기만 한다. 고치거나 지우지 않는다.
- git 커밋은 하지 않는다. 팩토리가 시도마다 커밋한다.
- 제품은 단일 파일 app.py로 유지한다. 의존성은 파일 머리의 PEP 723 인라인 메타데이터로 선언하고,
  `uv run app.py --port <port> --data-dir <dir>`로 실행되게 한다.
- 3D 장면 생성은 app.py가 Codex를 헤드리스로 호출해서 한다 (`codex exec`, 이미지는 `-i`).
  평가기가 CODEX_HOME, AWS_PROFILE 등 환경 변수를 넘기므로 os.environ을 그대로 자식 프로세스에 넘긴다.
  CODEX_HOME의 설정 파일은 고치지 않는다.
- three.js는 .factory/vendor/의 사본(three.module.js, three.core.js)을 저장소에 복사해 쓴다. 외부 CDN은 막혀 있다.
- 평가기는 정답이 공개된 공개 세트(.factory/fixtures/) 외에, 매번 새로 뽑는 비공개 홀드아웃 장면
  (그림자, 질감·이미지 노이즈 포함)으로도 채점한다. 공개 세트의 정답을 코드에 넣지 않는다.

검증
- 공개 세트로 공식 평가를 직접 돌릴 수 있다: {self_eval}
  이 명령은 Codex를 장면마다 한 번(공개 세트 4회) 호출하므로 시간과 비용이 든다. 필요할 때만 쓴다.
- 끝내기 전에 앱이 계약대로 뜨고 요청 하나가 done까지 가는지 확인한다.

마지막으로 .factory/attempt-notes.md에 이번 시도에서 바꾼 것, 확인한 것, 남은 문제의 원인 추정을 짧게 적는다.
"""

IMPLEMENT_TASK = """\
작업 명세 {work_item_id}를 구현하라.

{work_item_yaml}
수용 조건(acceptance)을 모두 만족해야 eval을 통과한다. 이번은 {attempt}/{max_attempts}번째 시도다.
{feedback_hint}"""


def load_work_item(path: Path) -> dict:
    import yaml
    item = yaml.safe_load(path.read_text())
    for key in ("work_item", "goal", "acceptance"):
        if key not in item:
            raise RuntimeError(f"작업 명세에 {key}가 없다: {path}")
    return item


def git(*args: str, cwd: Path, check: bool = True, identity: bool = False) -> str:
    env = {**os.environ, **FACTORY_GIT_IDENTITY} if identity else None
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out.stderr.strip()}")
    return out.stdout.strip()


def prepare_worktree(work_id: str) -> tuple[Path, str]:
    """이슈별 브랜치와 worktree. 이미 있으면 이어서 쓴다."""
    branch = f"factory/{work_id}"
    path = WORKTREES / work_id
    exclude = PRODUCT / ".git" / "info" / "exclude"
    if ".factory/" not in (exclude.read_text() if exclude.exists() else ""):
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a") as f:
            f.write("\n.factory/\n")
    if path.exists():
        return path, branch
    WORKTREES.mkdir(parents=True, exist_ok=True)
    exists = git("branch", "--list", branch, cwd=PRODUCT)
    git("worktree", "add", str(path), *([branch] if exists else ["-b", branch, "main"]), cwd=PRODUCT)
    return path, branch


def write_context(wt: Path, item: dict, lab: Lab) -> None:
    """작업 공간의 .factory/: 작업 명세, 계약, 공개 세트(이미지와 정답), three.js 사본."""
    import yaml
    ctx = wt / ".factory"
    (ctx / "fixtures").mkdir(parents=True, exist_ok=True)
    (ctx / "vendor").mkdir(exist_ok=True)
    (ctx / "work-item.yaml").write_text(yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
    shutil.copy(ROOT / "CONTRACT.md", ctx / "CONTRACT.md")
    for f in ("three.module.js", "three.core.js", "LICENSE"):
        shutil.copy(VENDOR / f, ctx / "vendor" / f)
    for fx in make_fixtures("public"):
        d = ctx / "fixtures" / fx.id
        if (d / "input.png").exists():
            continue
        lab.render(fx)
        d.mkdir(exist_ok=True)
        (d / "input.png").write_bytes(fx.png)
        (d / "gt.json").write_text(json.dumps(fx.spec, indent=2))


def run_claude(wt: Path, prompt: str, system: str, log_path: Path) -> dict:
    """Claude Code를 비대화식으로 실행한다. Bedrock default profile, 모델 지정 없음, 개인 설정 없음."""
    cmd = ["claude", "-p", prompt, "--bare", "--append-system-prompt", system,
           "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions", "--max-budget-usd", str(CLAUDE_BUDGET_PER_ATTEMPT_USD)]
    started = time.time()
    with open(log_path, "w") as log:
        try:
            proc = subprocess.run(cmd, cwd=wt, env=agent_env(), stdout=log, stderr=subprocess.STDOUT,
                                  timeout=CLAUDE_TIMEOUT_S)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    events = []
    for line in log_path.read_text(errors="replace").splitlines():
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    tool_calls = sum(1 for e in events if e.get("type") == "assistant"
                     for c in e.get("message", {}).get("content", []) if c.get("type") == "tool_use")
    return {"exit": code, "model": init.get("model"), "cost_usd": result.get("total_cost_usd"),
            "turns": result.get("num_turns"), "tool_calls": tool_calls,
            "seconds": round(time.time() - started), "subtype": result.get("subtype"),
            "is_error": result.get("is_error"), "summary": str(result.get("result", ""))[-1500:]}


def _check(op: str, actual: float | None, threshold: float) -> bool:
    if actual is None:
        return False
    return actual <= threshold if op == "<=" else actual >= threshold


def _parse_threshold(expr: str) -> tuple[str, float]:
    expr = str(expr).strip()
    op = expr[:2]
    if op not in ("<=", ">="):
        raise RuntimeError(f"목표값은 '<= 숫자' 또는 '>= 숫자' 형식이어야 한다: {expr}")
    return op, float(expr[2:])


def judge(item: dict, candidate: dict | None, baseline: dict | None, start_error: str | None) -> dict:
    """작업 명세의 수용 조건을 하나씩 판정한다. 모두 통과해야 eval 통과다."""
    conditions, failures = [], []

    def cond(name: str, ok: bool, detail: str):
        conditions.append({"name": name, "ok": ok, "detail": detail})

    for raw in item["acceptance"]:
        if raw == "run":
            cond("run", start_error is None, start_error or "app.py가 계약대로 시작됐다")
        elif raw == "contract-smoke":
            if candidate is None:
                cond("contract-smoke", False, "앱이 시작되지 않아 검사하지 못했다")
            else:
                v = candidate["smoke"]["violations"]
                cond("contract-smoke", not v, "계약 위반 없음" if not v else
                     "; ".join(f"{k}: {c[0]}" for k, c in v.items()))
        elif raw == "holdout-regression":
            if baseline is None:
                cond("holdout-regression", True, "비교할 main이 없어 생략했다")
            elif candidate is None or "holdout" not in candidate:
                cond("holdout-regression", False, "후보의 홀드아웃 점수가 없다")
            else:
                c, b = candidate["holdout"]["fidelity"], baseline["holdout"]["fidelity"]
                cond("holdout-regression", c >= b - REGRESSION_TOLERANCE,
                     f"후보 {c:.1f} / main {b:.1f} (같은 시드, 허용폭 {REGRESSION_TOLERANCE})")
        elif isinstance(raw, dict) and "suite" in raw:
            suite = raw["suite"]
            block = (candidate or {}).get(suite if suite in ("public", "holdout") else "overall")
            for metric, expr in raw.get("metrics", {}).items():
                op, threshold = _parse_threshold(expr)
                actual = block["metrics"].get(metric) if block else None
                ok = _check(op, actual, threshold)
                cond(f"{suite}:{metric}", ok, f"{actual} (목표 {op} {threshold})")
                if not ok and candidate:
                    worst = sorted(
                        (r for r in candidate["fixtures"] if suite in ("all", "overall") or r["variant"] == suite),
                        key=lambda r: r["metrics"].get(metric, 0), reverse=(op == "<="))[:3]
                    failures.append({"metric": metric, "suite": suite, "expected": f"{op} {threshold}",
                                     "actual": actual,
                                     "worst_fixtures": [{"fixture": r["fixture"], "seed": r["seed"],
                                                         "value": r["metrics"].get(metric),
                                                         "status": r["status"], "error": r["error"]}
                                                        for r in worst]})
        else:
            raise RuntimeError(f"알 수 없는 수용 조건: {raw}")
    return {"passed": all(c["ok"] for c in conditions), "conditions": conditions, "failures": failures}


def write_feedback(wt: Path, attempt: int, verdict: dict, candidate: dict | None,
                   baseline: dict | None, eval_dir: Path, app_log: Path | None) -> None:
    """실패 증거를 다음 시도의 .factory/feedback.json으로 넘긴다. 공개 세트만 이미지를 함께 준다."""
    ctx = wt / ".factory"
    fb_dir = ctx / "feedback"
    shutil.rmtree(fb_dir, ignore_errors=True)
    fb_dir.mkdir(parents=True)
    for f in verdict["failures"]:
        for w in f["worst_fixtures"]:
            src = eval_dir / w["fixture"] / "viewer.png"
            if w["fixture"].startswith("public") and src.exists():
                shutil.copy(src, fb_dir / f"{w['fixture']}-viewer.png")
                w["evidence"] = [f".factory/fixtures/{w['fixture']}/input.png",
                                 f".factory/feedback/{w['fixture']}-viewer.png"]
    feedback = {
        "attempt": attempt, "verdict": "failed",
        "conditions": verdict["conditions"], "failures": verdict["failures"],
        "score": {"public": (candidate or {}).get("public", {}).get("fidelity"),
                  "holdout": (candidate or {}).get("holdout", {}).get("fidelity"),
                  "main_same_seeds": (baseline or {}).get("holdout", {}).get("fidelity")},
        "smoke_violations": (candidate or {}).get("smoke", {}).get("violations", {}),
    }
    if app_log and app_log.exists():
        tail = app_log.read_text(errors="replace")[-4000:]
        (fb_dir / "app.log.tail").write_text(tail)
        feedback["app_log"] = ".factory/feedback/app.log.tail"
    notes = ctx / "attempt-notes.md"
    if notes.exists():
        feedback["previous_notes"] = notes.read_text()[-3000:]
    (ctx / "feedback.json").write_text(json.dumps(feedback, indent=2, ensure_ascii=False))


def evaluate_candidate(wt: Path, eval_dir: Path, lab: Lab) -> tuple[dict | None, dict | None, str | None]:
    """후보와 현재 main을 같은 홀드아웃 시드로 평가한다. (후보 요약, main 요약, 시작 오류)"""
    fixtures = make_fixtures("all")
    holdout = [fx for fx in fixtures if fx.variant == "holdout"]
    candidate = baseline = None
    start_error = None
    try:
        app = start_product_app(wt, eval_dir / "candidate" / "data", eval_dir / "candidate-app.log")
    except RuntimeError as e:
        start_error = str(e)
    else:
        try:
            candidate = run_eval(app, fixtures, eval_dir / "candidate", lab)
        finally:
            app.stop()
    if (PRODUCT / "app.py").exists() and candidate is not None:
        try:
            main_app = start_product_app(PRODUCT, eval_dir / "main" / "data", eval_dir / "main-app.log")
        except RuntimeError as e:
            print(f"  main을 띄우지 못해 회귀 비교를 생략한다: {e}")
        else:
            try:
                baseline = run_eval(main_app, holdout, eval_dir / "main", lab)
            finally:
                main_app.stop()
    return candidate, baseline, start_error


def commit_attempt(wt: Path, message: str) -> str | None:
    git("add", "-A", cwd=wt)
    if not git("status", "--porcelain", cwd=wt):
        return None
    git("commit", "-q", "-m", message, cwd=wt, identity=True)
    return git("rev-parse", "--short", "HEAD", cwd=wt)


def merge_and_deploy(item: dict, wt: Path, branch: str, best_commit: str) -> dict:
    """eval을 통과한 시도를 main에 squash 병합하고, main을 로컬 서버로 다시 띄운다."""
    git("checkout", "-q", branch, cwd=wt)
    git("reset", "-q", "--hard", best_commit, cwd=wt)
    previous = git("rev-parse", "--short", "main", cwd=PRODUCT)
    git("merge", "-q", "--squash", branch, cwd=PRODUCT)
    goal = " ".join(str(item["goal"]).split())[:72]
    git("commit", "-q", "-m", f"{item['work_item']}: {goal}\n\neval 통과 (factory run, 후보 {best_commit})",
        cwd=PRODUCT, identity=True)
    merged = git("rev-parse", "--short", "HEAD", cwd=PRODUCT)
    try:
        deploy()
    except RuntimeError as e:  # 배포가 실패하면 main을 되돌린다
        git("reset", "-q", "--hard", previous, cwd=PRODUCT)
        deploy(allow_missing=True)
        return {"merged": None, "deployed": False, "error": str(e), "previous": previous}
    git("worktree", "remove", "--force", str(wt), cwd=PRODUCT)
    return {"merged": merged, "deployed": True, "previous": previous, "url": f"http://127.0.0.1:{PROD_PORT}"}


def deploy(allow_missing: bool = False) -> None:
    """product/ main의 app.py를 PROD_PORT에서 다시 띄운다. 이 서버가 이 실험의 프로덕션이다."""
    pid_file = VAR / "prod.pid"
    if pid_file.exists():
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
            time.sleep(1)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        pid_file.unlink()
    if not (PRODUCT / "app.py").exists():
        if allow_missing:
            return
        raise RuntimeError("product/app.py가 없다")
    log = open(VAR / "prod.log", "a")
    proc = subprocess.Popen(
        ["uv", "run", "app.py", "--port", str(PROD_PORT), "--data-dir", str(VAR / "prod-data")],
        cwd=PRODUCT, env=agent_env(CODEX_HOME=str(CODEX_HOME)),
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"배포한 app.py가 종료됐다 (exit {proc.returncode}, var/prod.log)")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PROD_PORT)) == 0:
                code, _ = _http("GET", f"http://127.0.0.1:{PROD_PORT}/api/requests/not-a-key")
                if code in (200, 404):
                    return
        time.sleep(0.5)
    raise RuntimeError(f"배포한 app.py가 {PROD_PORT} 포트에서 응답하지 않는다")


def run_work_item(item: dict, lab: Lab, log: Callable[[str], None] = print) -> dict:
    """작업 명세 하나를 끝까지 실행한다. 결과는 var/runs/<실행 ID>/run.json에 남는다."""
    import yaml
    work_id = str(item["work_item"])
    run_id = f"{work_id}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    wt, branch = prepare_worktree(work_id)
    write_context(wt, item, lab)
    self_eval = f"uv run {ROOT / 'factory.py'} eval --app . --suite public --label self-{work_id}"
    system = IMPLEMENT_SYSTEM.format(self_eval=self_eval)
    record = {"run_id": run_id, "work_item": work_id, "branch": branch, "worktree": str(wt),
              "harness": "v1", "attempts": [], "status": "running", "started": datetime.now().isoformat()}

    def save():
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))

    best = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        feedback = wt / ".factory" / "feedback.json"
        hint = ("이전 시도가 eval을 통과하지 못했다. .factory/feedback.json과 .factory/feedback/을 먼저 읽어라."
                if feedback.exists() else "첫 시도다.")
        prompt = IMPLEMENT_TASK.format(
            work_item_id=work_id, work_item_yaml=yaml.safe_dump(item, allow_unicode=True, sort_keys=False),
            attempt=attempt, max_attempts=MAX_ATTEMPTS, feedback_hint=hint)
        log(f"[{work_id}] 시도 {attempt}/{MAX_ATTEMPTS}: Claude Code 실행")
        claude = run_claude(wt, prompt, system, run_dir / f"attempt-{attempt}.jsonl")
        log(f"[{work_id}]   Claude Code 끝: {claude['seconds']}초, ${claude['cost_usd'] or 0:.2f}, "
            f"도구 호출 {claude['tool_calls']}회, model={claude['model']}")
        commit = commit_attempt(wt, f"{work_id} attempt {attempt}")
        eval_dir = run_dir / f"attempt-{attempt}-eval"
        log(f"[{work_id}]   평가 시작 (커밋 {commit})")
        candidate, baseline, start_error = evaluate_candidate(wt, eval_dir, lab)
        verdict = judge(item, candidate, baseline, start_error)
        entry = {"attempt": attempt, "commit": commit, "claude": claude, "eval_dir": str(eval_dir),
                 "verdict": verdict,
                 "score": {"public": (candidate or {}).get("public", {}).get("fidelity"),
                           "holdout": (candidate or {}).get("holdout", {}).get("fidelity"),
                           "main_same_seeds": (baseline or {}).get("holdout", {}).get("fidelity")},
                 "codex_calls": sum(len(s["fixtures"]) for s in (candidate, baseline) if s),
                 "generation_seconds": [r["seconds"] for s in (candidate, baseline) if s
                                        for r in s["fixtures"] if r.get("seconds") is not None]}
        record["attempts"].append(entry)
        save()
        for c in verdict["conditions"]:
            log(f"[{work_id}]   {'통과' if c['ok'] else '실패'}  {c['name']}: {c['detail']}")
        if commit and (best is None or (entry["score"]["public"] or 0) > (best["score"]["public"] or 0)):
            best = entry
        if verdict["passed"]:
            log(f"[{work_id}]   eval 통과 → 병합과 배포")
            record["delivery"] = merge_and_deploy(item, wt, branch, commit or git("rev-parse", "--short", "HEAD", cwd=wt))
            record["status"] = "done" if record["delivery"]["deployed"] else "needs_human"
            break
        write_feedback(wt, attempt, verdict, candidate, baseline, eval_dir,
                       eval_dir / "candidate-app.log")
    else:
        record["status"] = "needs_human"
        record["best_attempt"] = best["attempt"] if best else None
        if best and best["commit"]:  # 최고점 시도를 브랜치 끝에 남겨 사람이 볼 수 있게 한다
            git("reset", "-q", "--hard", best["commit"], cwd=wt)
    record["finished"] = datetime.now().isoformat()
    record["cost_usd"] = round(sum(a["claude"]["cost_usd"] or 0 for a in record["attempts"]), 4)
    record["codex_calls"] = sum(a["codex_calls"] for a in record["attempts"])
    save()
    log(f"[{work_id}] 결과: {record['status']} (Claude Code ${record['cost_usd']:.2f}, "
        f"Codex 호출 {record['codex_calls']}회) → {run_dir.relative_to(ROOT)}/run.json")
    return record


def cmd_run(args: argparse.Namespace) -> int:
    item = load_work_item(Path(args.work_item))
    lab = Lab()
    try:
        record = run_work_item(item, lab)
    finally:
        lab.close()
    return 0 if record["status"] == "done" else 1


# ---------------------------------------------------------------------------
# doctor: 이후 단계가 실패할 원인을 미리 찾는다
# ---------------------------------------------------------------------------

def check_tools() -> str:
    found = []
    for tool in ("git", "uv", "claude", "codex", "aws"):
        path = shutil.which(tool)
        if not path:
            raise RuntimeError(f"{tool} 없음")
        found.append(tool)
    return ", ".join(found)


def check_aws() -> str:
    out = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--profile", "default", "--query", "Arn"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return f"default profile 자격 증명 정상 (region {aws_default_region()})"


def check_claude() -> str:
    """Claude Code를 비대화식으로 한 번 부른다. --bare로 개인 설정을 끊는다."""
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            ["claude", "-p", "Reply with exactly the word PONG.", "--bare",
             "--output-format", "stream-json", "--verbose", "--max-budget-usd", "0.5"],
            cwd=tmp, env=agent_env(), capture_output=True, text=True, timeout=300,
        )
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    if out.returncode != 0 or "PONG" not in str(result.get("result", "")):
        detail = result.get("result") or out.stderr.strip() or out.stdout.strip()
        raise RuntimeError(f"응답 이상: {str(detail)[:300]}")
    record_doctor("claude", {"model": init.get("model"), "cost_usd": result.get("total_cost_usd")})
    return f"model={init.get('model')} cost=${result.get('total_cost_usd', 0):.4f}"


def check_codex() -> str:
    """제품과 같은 방식(전용 CODEX_HOME, 헤드리스)으로 Codex를 한 번 부른다."""
    if not (CODEX_HOME / "config.toml").exists():
        raise RuntimeError("var/codex-home이 없다 (먼저 init)")
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--json",
             "-C", tmp, "Reply with exactly the word PONG."],
            env=agent_env(CODEX_HOME=str(CODEX_HOME)),
            capture_output=True, text=True, timeout=300,
        )
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    text = " ".join(
        e.get("item", {}).get("text", "") for e in events if e.get("type") == "item.completed"
    )
    usage = next((e.get("usage") for e in events if e.get("type") == "turn.completed"), None)
    if out.returncode != 0 or "PONG" not in text:
        raise RuntimeError(f"응답 이상: {(out.stderr or out.stdout).strip()[-300:]}")
    model = tomllib.loads((CODEX_HOME / "config.toml").read_text()).get("model")
    record_doctor("codex", {"model": model, "usage": usage})
    return f"model={model} usage={usage}"


def check_browser() -> str:
    """합성 장면 렌더링에 필요한 헤드리스 Chromium과 WebGL을 확인한다."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        page = browser.new_page()
        webgl = page.evaluate("!!document.createElement('canvas').getContext('webgl2')")
        version = browser.version
        browser.close()
    if not webgl:
        raise RuntimeError("WebGL2 컨텍스트를 만들 수 없다")
    return f"chromium {version}, WebGL2 사용 가능"


def check_local() -> str:
    missing = [str(p.relative_to(ROOT)) for p in
               (CODEX_HOME / "config.toml", VENDOR / "three.module.js", PRODUCT / ".git")
               if not p.exists()]
    if missing:
        raise RuntimeError(f"없음: {', '.join(missing)} (먼저 init)")
    return "codex-home, vendor/three.js, product 저장소"


def check_linear() -> str:
    team = linear_team(team_key())
    labels = {l["name"] for l in team["labels"]["nodes"]}
    states = {s["name"] for s in team["states"]["nodes"]}
    missing = [n for n in LINEAR_LABELS if n not in labels]
    if NEEDS_HUMAN not in states:
        missing.append(f"상태 '{NEEDS_HUMAN}'")
    if missing:
        raise RuntimeError(f"팀 {team['key']} 연결은 되지만 준비 안 됨: {', '.join(missing)} (linear-setup)")
    return f"팀 {team['key']} ({team['name']}) 라벨·상태 준비 완료"


def record_doctor(name: str, value: dict) -> None:
    path = VAR / "doctor.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = value
    VAR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("도구", check_tools),
        ("로컬 준비", check_local),
        ("AWS", check_aws),
        ("Chromium", check_browser),
        ("Linear", check_linear),
        ("Claude Code", check_claude),
        ("Codex", check_codex),
    ]
    if args.skip_agents:
        checks = [c for c in checks if c[0] not in ("Claude Code", "Codex")]
    failed = 0
    for name, check in checks:
        try:
            print(f"OK    {name:<12} {check()}", flush=True)
        except Exception as e:  # 점검은 하나가 실패해도 나머지를 계속한다
            failed += 1
            print(f"FAIL  {name:<12} {e}", flush=True)
    print(f"\n{len(checks) - failed}/{len(checks)} 통과")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# linear-setup: 라벨, Needs Human 상태, 읽기·쓰기 시험
# ---------------------------------------------------------------------------

def cmd_linear_setup(args: argparse.Namespace) -> int:
    team = linear_team(args.team or team_key())
    labels = {l["name"] for l in team["labels"]["nodes"]}
    for name, (color, description) in LINEAR_LABELS.items():
        if name in labels:
            print(f"있음  라벨 {name}")
            continue
        linear(
            """mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) { success } }""",
            {"input": {"teamId": team["id"], "name": name, "color": color,
                       "description": description}},
        )
        print(f"생성  라벨 {name}")

    if any(s["name"] == NEEDS_HUMAN for s in team["states"]["nodes"]):
        print(f"있음  상태 {NEEDS_HUMAN}")
    else:
        linear(
            """mutation($input: WorkflowStateCreateInput!) {
              workflowStateCreate(input: $input) { success } }""",
            {"input": {"teamId": team["id"], "name": NEEDS_HUMAN, "type": "started",
                       "color": "#f2994a",
                       "description": "eval을 통과하지 못해 사람의 판단을 기다리는 이슈"}},
        )
        print(f"생성  상태 {NEEDS_HUMAN} (type=started)")

    # 읽기·쓰기 시험: 이슈를 만들고 댓글을 단 뒤 지운다
    issue = linear(
        """mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) { issue { id identifier } } }""",
        {"input": {"teamId": team["id"], "title": "[factory] linear-setup 연결 시험",
                   "description": "factory.py linear-setup이 만든 시험 이슈. 곧 삭제된다."}},
    )["issueCreate"]["issue"]
    linear(
        """mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }""",
        {"input": {"issueId": issue["id"], "body": "댓글 쓰기 시험"}},
    )
    linear("""mutation($id: String!) { issueDelete(id: $id) { success } }""", {"id": issue["id"]})
    print(f"시험  이슈 {issue['identifier']} 생성 → 댓글 → 삭제 성공")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="factory.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="로컬 준비: codex-home, three.js 사본, 제품 저장소, Chromium")
    p = sub.add_parser("doctor", help="환경 점검 (Claude Code·Codex를 한 번씩 호출한다)")
    p.add_argument("--skip-agents", action="store_true", help="모델 호출 점검을 건너뛴다")
    p = sub.add_parser("linear-setup", help="Linear 라벨·Needs Human 상태 준비와 읽기·쓰기 시험")
    p.add_argument("--team", help="Linear 팀 키 (기본 FACTORY_LINEAR_TEAM 또는 SWF)")
    p = sub.add_parser("eval", help="제품 폴더의 app.py를 띄워 정답 장면으로 평가한다")
    p.add_argument("--app", default=str(PRODUCT), help="app.py가 있는 폴더 (기본 product/)")
    p.add_argument("--suite", choices=["public", "holdout", "all"], default="all")
    p.add_argument("--label", help="결과 폴더 이름에 붙일 라벨")
    p.add_argument("--holdout-seeds", help="홀드아웃 시드를 고정한다 (쉼표 구분, 점수 흔들림 측정용)")
    sub.add_parser("calibrate", help="기준 앱(정답·빈 장면·오차 주입)으로 채점기를 검증한다")
    p = sub.add_parser("run", help="로컬 작업 명세 하나를 구현·평가·병합까지 실행한다")
    p.add_argument("work_item", help="작업 명세 YAML 경로 (예: work/LOCAL-1.yaml)")

    args = parser.parse_args()
    handlers = {"init": cmd_init, "doctor": cmd_doctor, "linear-setup": cmd_linear_setup,
                "eval": cmd_eval, "calibrate": cmd_calibrate, "run": cmd_run}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
