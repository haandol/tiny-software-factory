"""요청 기록 저장소: 키마다 상태(pending/done/failed)를 <data-dir>/requests/<키>.json에 원자적으로 쓰고 메모리에 둔다."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        for sub in ("requests", "uploads", "scenes", "work"):
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.records: dict[str, dict] = {}
        for f in (data_dir / "requests").glob("*.json"):
            try:
                self.records[f.stem] = json.loads(f.read_text())
            except ValueError:
                pass

    def get(self, key: str) -> dict | None:
        with self.lock:
            rec = self.records.get(key)
            return dict(rec) if rec else None

    def put(self, key: str, **fields) -> dict:
        with self.lock:
            rec = self.records.setdefault(key, {"key": key})
            rec.update(fields)
            tmp = self.dir / "requests" / f"{key}.json.tmp"
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
            tmp.replace(self.dir / "requests" / f"{key}.json")
            return dict(rec)

    def pending(self) -> list[str]:
        with self.lock:
            return [k for k, r in self.records.items() if r.get("status") == "pending"]
