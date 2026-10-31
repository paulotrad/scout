"""One-day, ID-only comparison of fast-check collection depths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import median
import uuid


ROOT = Path(__file__).resolve().parent
PILOT_FILE = ROOT / "runs" / "fast_coverage_pilot.json"
SCAN_ROOT = ROOT / "runs" / "scans"
DEPTHS = (12, 40, 80)


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_pilot(path: Path = PILOT_FILE) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start_pilot(account_id: str, watch_item: dict, *,
                now: datetime | None = None, path: Path = PILOT_FILE,
                reset: bool = False, silent: bool = False) -> dict:
    """Start once; repeated setup calls cannot extend the comparison window."""
    existing = load_pilot(path)
    if existing and existing.get("status") == "running" and not reset:
        return existing
    now = now or datetime.now(timezone.utc)
    state = {
        "id": "coverage-" + uuid.uuid4().hex[:12],
        "status": "running", "account_id": account_id, "silent": silent,
        "watch_item": {key: watch_item[key] for key in (
            "term", "zip_code", "radius_miles", "min_price", "max_price")},
        "depths": list(DEPTHS), "started_at": now.isoformat(),
        "ends_at": (now + timedelta(hours=24)).isoformat(),
    }
    _write(path, state)
    return state


def stop_pilot(reason: str, *, path: Path = PILOT_FILE,
               now: datetime | None = None) -> dict | None:
    """End a running depth experiment without changing the user's watchlist."""
    state = load_pilot(path)
    if not state or state.get("status") != "running":
        return state
    state["status"] = "cancelled"
    state["stopped_at"] = (now or datetime.now(timezone.utc)).isoformat()
    state["stop_reason"] = reason[:200]
    _write(path, state)
    return state


def _matches(state: dict, manifest: dict) -> bool:
    saved = state.get("watch_item") or {}
    current = manifest.get("watch_item") or {}
    return (manifest.get("mode") == "incremental" and
            (manifest.get("pilot_owner_id") or manifest.get("account_id")) ==
            state.get("account_id") and
            str(current.get("term") or "").casefold() ==
            str(saved.get("term") or "").casefold() and
            all(current.get(key) == saved.get(key) for key in (
                "zip_code", "radius_miles", "min_price", "max_price")))


def finish_pilot_if_due(*, now: datetime | None = None,
                        path: Path = PILOT_FILE,
                        scan_root: Path = SCAN_ROOT) -> dict | None:
    state = load_pilot(path)
    if not state or state.get("status") != "running":
        return state
    now = now or datetime.now(timezone.utc)
    if now < _date(state["ends_at"]):
        return state
    runs = []
    for file in scan_root.glob("scan-*/manifest.json"):
        try:
            manifest = json.loads(file.read_text(encoding="utf-8"))
            probe = manifest.get("coverage_probe") or {}
            if (probe.get("pilot_id") != state["id"] and
                    manifest.get("coverage_pilot_id") != state["id"]):
                continue
            if manifest.get("status") in {"queued", "running"}:
                return state
            runs.append((manifest, probe))
        except (OSError, ValueError, TypeError):
            continue
    unique = {depth: set() for depth in DEPTHS}
    times = {depth: [] for depth in DEPTHS}
    for _manifest, probe in runs:
        for row in probe.get("depths") or []:
            depth = row.get("depth")
            if depth not in unique:
                continue
            unique[depth].update(str(value) for value in
                                 row.get("new_listing_ids") or [])
            if isinstance(row.get("seconds_to_depth"), (int, float)):
                times[depth].append(row["seconds_to_depth"])
    count_at_80 = len(unique[80])
    completed = sum(manifest.get("status") == "complete" for manifest, _ in runs)
    max_cards_seen = max((int(probe.get("cards_seen") or 0)
                          for _, probe in runs), default=0)
    times_to_stop = [probe["seconds_to_stop"] for _, probe in runs
                     if isinstance(probe.get("seconds_to_stop"), (int, float))]
    sufficient = len(runs) >= 4 and completed >= max(3, len(runs) * 0.8)
    chosen = (next((depth for depth in DEPTHS if
                    len(unique[depth]) >= count_at_80 * 0.95), 80)
              if sufficient and count_at_80 else 12)
    state["status"] = "complete"
    state["report"] = {
        "runs": len(runs), "completed_runs": completed,
        "max_cards_seen": max_cards_seen,
        "median_seconds_to_stop": (round(median(times_to_stop), 1)
                                   if times_to_stop else None),
        "confidence": "measured" if sufficient else "insufficient_data",
        "chosen_depth": chosen,
        "depths": [{"depth": depth, "unique_new_ids": len(unique[depth]),
                    "median_seconds_to_depth": (
                        round(median(times[depth]), 1) if times[depth] else None)}
                   for depth in DEPTHS],
        "note": ("Too few completed checks; keep the 12-card baseline."
                 if not sufficient else
                 "Marketplace showed fewer than 80 cards in every check; the choice compares all visible cards."
                 if max_cards_seen < 80 else
                 "Smallest depth that saw at least 95% of new IDs found by 80 cards."),
    }
    _write(path, state)
    return state


def fast_scan_policy(manifest: dict, *, now: datetime | None = None,
                     path: Path = PILOT_FILE,
                     scan_root: Path = SCAN_ROOT) -> dict:
    """Default to 12; compare up to 80 only for the pilot's exact search."""
    state = finish_pilot_if_due(now=now, path=path, scan_root=scan_root)
    if not state or not _matches(state, manifest):
        return {"max_items": 12, "scrolls": 1, "pilot_id": None,
                "depths": []}
    if (state.get("status") == "running" and state.get("silent") and
            not manifest.get("silent_pilot")):
        return {"max_items": 12, "scrolls": 1, "pilot_id": None,
                "depths": []}
    if state["status"] == "running":
        return {"max_items": 80, "scrolls": 8, "pilot_id": state["id"],
                "depths": list(DEPTHS)}
    chosen = int(state.get("report", {}).get("chosen_depth") or 12)
    return {"max_items": chosen, "scrolls": 1 if chosen == 12 else 8,
            "pilot_id": None, "depths": []}


def public_pilot_status(account_id: str, *,
                        watch_items: list[dict] | None = None,
                        path: Path = PILOT_FILE,
                        scan_root: Path = SCAN_ROOT) -> dict | None:
    state = load_pilot(path)
    if not state or state.get("account_id") != account_id:
        return None
    result = {key: state.get(key) for key in (
        "status", "started_at", "ends_at", "stopped_at", "stop_reason",
        "depths", "report", "silent")}
    result["term"] = (state.get("watch_item") or {}).get("term")
    result["paused"] = (state.get("status") == "running" and not state.get("silent") and
                        not any(item.get("fast") and _matches(state, {
                            "mode": "incremental", "account_id": account_id,
                            "watch_item": item}) for item in (watch_items or [])))
    latest = None
    for file in scan_root.glob("scan-*/manifest.json"):
        try:
            manifest = json.loads(file.read_text(encoding="utf-8"))
            probe = manifest.get("coverage_probe") or {}
            if probe.get("pilot_id") != state["id"]:
                continue
            if latest is None or str(manifest.get("created_at") or "") > str(
                    latest.get("created_at") or ""):
                latest = manifest
        except (OSError, ValueError, TypeError):
            continue
    result["latest_probe"] = latest.get("coverage_probe") if latest else None
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Start a 24-hour fast-check depth pilot")
    parser.add_argument("account_id")
    parser.add_argument("term")
    parser.add_argument("zip_code")
    parser.add_argument("radius_miles", type=int)
    parser.add_argument("min_price", type=float)
    parser.add_argument("max_price", type=float)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()
    print(json.dumps(start_pilot(args.account_id, vars(args), reset=args.reset,
                                 silent=args.silent),
                     indent=2))
