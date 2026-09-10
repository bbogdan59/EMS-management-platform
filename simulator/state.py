from __future__ import annotations

import json
from pathlib import Path


class SimulatorState:
    def __init__(self, state_dir: str):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, station_key: str) -> Path:
        return self.dir / f"{station_key}.json"

    def load(self, station_key: str) -> dict | None:
        p = self._path(station_key)
        if not p.exists():
            return None
        return json.loads(p.read_text())

    def save(self, station_key: str, data: dict) -> None:
        self._path(station_key).write_text(json.dumps(data, indent=2))
