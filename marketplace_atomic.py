"""Write scan checkpoints without exposing partial JSON to readers."""

from __future__ import annotations

import json
from pathlib import Path
import time
import uuid


def write_json(path: Path, data: object) -> None:
    """Retry a brief Windows sharing lock while keeping the old file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    try:
        for attempt in range(15):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 14:
                    raise
                time.sleep(min(0.05 * (attempt + 1), 0.4))
    finally:
        temporary.unlink(missing_ok=True)
