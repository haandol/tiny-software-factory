"""백그라운드 작업: 업로드를 정규화하고, Codex 측정(표본 여러 개) → 표본마다 장면 명세 → 가장 잘 맞는 표본 고르기
→ 장면 파일 저장을 최대 CODEX_TRIES번 시도한다."""

from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from .codex import measure
from .config import CODEX_SAMPLES, CODEX_TRIES, CONCURRENCY, log
from .consensus import pick
from .images import normalize_image
from .scene_file import scene_module
from .store import Store


class Worker:
    def __init__(self, store: Store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="previz")

    def submit(self, key: str) -> None:
        self.pool.submit(self._run, key)

    def _run(self, key: str) -> None:
        rec = self.store.get(key) or {}
        work = self.store.dir / "work" / key
        work.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            image = work / "input.png"
            W, H = normalize_image((self.store.dir / "uploads" / rec["upload"]).read_bytes(), image)
        except Exception as e:
            log(f"[{key}] 이미지를 읽지 못했다: {e}")
            self.store.put(key, status="failed", error=f"이미지를 읽지 못했다: {e}", finished=time.time())
            return
        last = "알 수 없는 오류"
        for attempt in range(1, CODEX_TRIES + 1):
            try:
                self._attempt(key, attempt, image, work, W, H, t0)
                return
            except Exception as e:
                last = str(e)[:500]
                log(f"[{key}] 시도 {attempt}/{CODEX_TRIES} 실패: {last}")
                (work / f"error-{attempt}.txt").write_text(traceback.format_exc())
        self.store.put(key, status="failed", error=last, finished=time.time())

    def _attempt(self, key: str, attempt: int, image: Path, work: Path, W: int, H: int, t0: float) -> None:
        """Codex 측정 한 번(표본 CODEX_SAMPLES개)으로 장면 파일을 만들고 done으로 기록한다. 실패하면 예외를 낸다."""
        analyses = measure(image, work, W, H, CODEX_SAMPLES)
        for sample, analysis in analyses:
            (work / f"analysis-{attempt}-{sample}.json").write_text(json.dumps(analysis, indent=1, ensure_ascii=False))
        if not any(a.get("objects") for _, a in analyses) and attempt < CODEX_TRIES:
            raise RuntimeError("Codex가 주요 물체를 하나도 찾지 못했다")    # 한 번 더 물어본다
        best, candidates = pick(analyses, W, H, np.asarray(Image.open(image).convert("RGB")))
        (work / f"analysis-{attempt}.json").write_text(json.dumps(best.analysis, indent=1, ensure_ascii=False))
        spec = best.spec
        spec["source"] = {"codex_attempt": attempt, "seconds": round(time.time() - t0, 1), "sample": best.sample,
                          "samples": {c.sample: round(c.cost, 4) for c in candidates}}
        self._write_scene(key, spec)
        self.store.put(key, status="done", error=None, finished=time.time(),
                       seconds=round(time.time() - t0, 1), objects=len(spec["objects"]))
        ci = spec["camera_info"]
        costs = " ".join(f"#{c.sample}:{c.cost:.3f}/{c.spec['camera_info']['vfov']:.0f}°" for c in candidates)
        log(f"[{key}] done {time.time() - t0:.0f}초: 표본 #{best.sample} ({costs}) fov {ci['vfov']:.1f} "
            f"pitch {ci['pitch']:.1f} roll {ci['roll']:.1f}, 물체 {len(spec['objects'])}개, 벽 {spec['room']['walls_measured']}")

    def _write_scene(self, key: str, spec: dict) -> None:
        """장면 파일을 임시 파일에 다 쓴 뒤 바꿔 넣는다 (done은 그 다음에만 기록한다)."""
        out = self.store.dir / "scenes" / f"{key}.js"
        tmp = out.with_suffix(".js.tmp")
        tmp.write_text(scene_module(spec, key))
        tmp.replace(out)
