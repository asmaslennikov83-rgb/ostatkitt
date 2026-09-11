from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


def cleanup_history(root: Path, retention_days: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for item in root.iterdir():
        if not item.is_dir():
            continue
        try:
            created = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
            if created < cutoff:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            pass


def make_run_dir(root: Path, user_id: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = root / f"{stamp}_{user_id}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
