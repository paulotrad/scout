"""Read-only scan health and a measured Scout benchmark window."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sqlite3
from statistics import median


BENCHMARK_FILE = Path(__file__).resolve().parent / "runs" / "scout_benchmark.json"
TERMINAL = {"complete", "failed", "interrupted"}


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _same_watch(scan: dict, watch: dict) -> bool:
    saved = scan.get("watch_item") or {}
    if not isinstance(saved, dict):
        return False
    if str(saved.get("term") or "").casefold() != str(watch.get("term") or "").casefold():
        return False
    return all(saved.get(key) == watch.get(key) for key in (
        "zip_code", "radius_miles", "min_price", "max_price"))


def _duration_minutes(scan: dict) -> float | None:
    start = _time(scan.get("created_at"))
    finish = _time(scan.get("completed_at") or scan.get("updated_at"))
    if start is None or finish is None or finish < start:
        return None
    return round((finish - start).total_seconds() / 60, 1)


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, (9 * len(ordered) + 9) // 10 - 1)], 1)


def load_benchmark(path: Path = BENCHMARK_FILE) -> dict | None:
    """The benchmark is explicitly started once; viewing it never resets time."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        start = _time(data.get("started_at"))
        end = _time(data.get("ends_at"))
        if start is None or end is None or end <= start:
            return None
        return {"started_at": start.isoformat(), "ends_at": end.isoformat(),
                "target_searches": int(data.get("target_searches") or 25)}
    except (OSError, ValueError, TypeError):
        return None


def scan_summaries(scan_root: Path) -> list[dict]:
    """Read only small manifests so the health page stays responsive."""
    rows = []
    for path in scan_root.glob("scan-*/manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id") == path.parent.name:
                rows.append(data)
        except (OSError, ValueError):
            continue
    return rows


def alert_events(search_id: int | None, db_path: Path) -> list[dict]:
    if not search_id or not db_path.is_file():
        return []
    try:
        with sqlite3.connect(db_path, timeout=5) as db:
            rows = db.execute(
                "SELECT scan_id, listing_id, matched_at FROM match_alerts "
                "WHERE search_id = ?", (search_id,)).fetchall()
        return [{"scan_id": row[0], "listing_id": row[1],
                 "matched_at": row[2]} for row in rows]
    except sqlite3.Error:
        return []


def feedback_events(account_id: str | None, db_path: Path) -> list[dict]:
    if not account_id or not db_path.is_file():
        return []
    try:
        with sqlite3.connect(db_path, timeout=5) as db:
            rows = db.execute(
                "SELECT listing_id, vote FROM item_feedback WHERE account_id = ?",
                (account_id,)).fetchall()
        return [{"listing_id": row[0], "vote": row[1]} for row in rows]
    except sqlite3.Error:
        return []


def _summary(scans: list[dict], alerts: list[dict], feedback: list[dict]) -> dict:
    terminal = [row for row in scans if row.get("status") in TERMINAL]
    complete = [row for row in terminal if row.get("status") == "complete"]
    failed = [row for row in terminal if row.get("status") != "complete"]
    durations = [value for row in complete
                 if (value := _duration_minutes(row)) is not None]
    by_scan = {row.get("id"): row for row in scans}
    matched = [row for row in alerts if row.get("scan_id") in by_scan]
    matched_ids = {str(row.get("listing_id")) for row in matched}
    rated = [row for row in feedback if str(row.get("listing_id")) in matched_ids]
    latencies = []
    for alert in matched:
        started = _time(by_scan[alert["scan_id"]].get("created_at"))
        sent = _time(alert.get("matched_at"))
        if started and sent and sent >= started:
            latencies.append((sent - started).total_seconds() / 60)
    return {
        "attempts": len(terminal), "complete": len(complete),
        "failed": len(failed),
        "completion_percent": (round(100 * len(complete) / len(terminal), 1)
                               if terminal else None),
        "cards_checked": sum(int(row.get("cards_checked") or
                                 row.get("scanned_count") or 0) for row in scans),
        "new_cards": sum(int(row.get("scanned_count") or 0) for row in scans
                         if row.get("mode") == "incremental"),
        "new_alerts": len(matched),
        "feedback_rated": len(rated),
        "feedback_needs_correction": sum(row.get("vote") == "down" for row in rated),
        "known_unfinished_analyses": sum(
            int(row.get("analysis_retry_pending_count") or 0) for row in complete),
        "median_scan_minutes": round(median(durations), 1) if durations else None,
        "p90_scan_minutes": _p90(durations),
        "median_scan_to_alert_minutes": (round(median(latencies), 1)
                                         if latencies else None),
        "p90_scan_to_alert_minutes": _p90(latencies),
    }


def health_report(account: dict, scans: list[dict], queue: list[dict],
                  alerts: list[dict], benchmark: dict | None,
                  feedback: list[dict] | None = None,
                  now: datetime | None = None) -> dict:
    """Keep old search settings out of current watch health and benchmark."""
    now = now or datetime.now(timezone.utc)
    feedback = feedback or []
    recent_start = now - timedelta(hours=48)
    watches = []
    enrolled_ids = set()
    for watch in account.get("watch_items") or []:
        history = sorted((row for row in scans if _same_watch(row, watch) and
                          row.get("mode") in {"scheduled", "incremental"} and
                          not row.get("silent_pilot")),
                         key=lambda row: _time(row.get("created_at")) or
                         datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        enrolled_ids.update(row.get("id") for row in history)
        latest = history[0] if history else None
        success = next((row for row in history if row.get("status") == "complete"), None)
        pending = next((row for row in queue if _same_watch(row, watch) and
                        row.get("status") in {"running", "queued"}), None)
        last_success = _time((success or {}).get("completed_at") or
                             (success or {}).get("updated_at"))
        next_times = [_time(account.get("next_run_at"))]
        if watch.get("fast"):
            next_times.append(_time(account.get("next_fast_run_at")))
        next_times = [value for value in next_times if value]
        next_due = min(next_times) if next_times else None
        allowed_gap = timedelta(hours=2 if watch.get("fast") else 30)
        due_grace = timedelta(minutes=20 if watch.get("fast") else 120)
        if pending:
            state, note = pending["status"], "A check is in progress or waiting its turn"
        elif latest and latest.get("status") in {"failed", "interrupted"}:
            state, note = "attention", "The last check failed"
        elif last_success is None:
            state, note = "waiting", "No completed check for these settings yet"
        elif (now - last_success > allowed_gap or
              (next_due is not None and now > next_due + due_grace)):
            state, note = "attention", "The last completed check is overdue"
        else:
            state, note = "on_track", "Recent checks are completing"
        recent = [row for row in history if (stamp := _time(row.get("created_at")))
                  and stamp >= recent_start]
        watches.append({
            "term": watch.get("term"), "zip_code": watch.get("zip_code"),
            "radius_miles": watch.get("radius_miles"),
            "min_price": watch.get("min_price"),
            "max_price": watch.get("max_price"),
            "fast": bool(watch.get("fast")), "state": state, "note": note,
            "next_scheduled_at": next_due.isoformat() if next_due else None,
            "last_attempt_at": (latest or {}).get("created_at"),
            "last_success_at": last_success.isoformat() if last_success else None,
            "last_scan_id": (latest or {}).get("id"),
            "last_error": ((latest or {}).get("error") if latest and
                           latest.get("status") in {"failed", "interrupted"} else None),
            "last_cards_checked": ((latest or {}).get("cards_checked") or
                                   (latest or {}).get("scanned_count")),
            "last_new_cards": (latest or {}).get("scanned_count"),
            "last_unfinished_analyses": (latest or {}).get("analysis_retry_pending_count"),
            "recent": _summary(recent, alerts, feedback),
        })
    current_scans = [row for row in scans if row.get("id") in enrolled_ids]
    recent_scans = [row for row in current_scans
                    if (stamp := _time(row.get("created_at"))) and
                    stamp >= recent_start]
    benchmark_data = {"status": "not_started", "competitor_status": "not_measured"}
    if benchmark:
        start = _time(benchmark["started_at"])
        end = _time(benchmark["ends_at"])
        window = [row for row in current_scans
                  if (stamp := _time(row.get("created_at"))) and
                  start <= stamp <= min(now, end)]
        benchmark_data = {
            **benchmark, "status": "complete" if now >= end else "collecting",
            "competitor_status": "not_measured",
            "enrolled_searches": len(watches),
            "metrics": _summary(window, alerts, feedback),
            "missing_measurements": [
                "Listings missed by Scout cannot be inferred from Scout scans alone",
                "Seller posting time is not collected, so listing-to-alert delay is unknown",
                "No competitor alerts have been recorded for the same searches",
            ],
        }
    return {"generated_at": now.isoformat(),
            "configured": bool(account.get("configured")), "window_hours": 48,
            "watches": watches, "recent": _summary(recent_scans, alerts, feedback),
            "benchmark": benchmark_data}


def benchmark_csv(scans: list[dict], watches: list[dict],
                  benchmark: dict | None, alerts: list[dict]) -> str:
    """Export exact Scout observations; blank competitor rows are never invented."""
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(("source", "keyword", "zip_code", "radius_miles",
                     "min_price", "max_price", "scan_id", "status",
                     "scan_started_at", "scan_finished_at", "duration_minutes",
                     "cards_checked", "new_cards", "new_alerts",
                     "unfinished_analyses", "error"))
    if not benchmark:
        return output.getvalue()
    start, end = _time(benchmark["started_at"]), _time(benchmark["ends_at"])
    alert_counts = {}
    for alert in alerts:
        alert_counts[alert.get("scan_id")] = alert_counts.get(alert.get("scan_id"), 0) + 1
    for scan in scans:
        stamp = _time(scan.get("created_at"))
        watch = next((item for item in watches if _same_watch(scan, item)), None)
        if (not watch or scan.get("silent_pilot") or
                scan.get("mode") not in {"scheduled", "incremental"}
                or not stamp or not start <= stamp <= end):
            continue
        writer.writerow(("Scout", watch.get("term"), watch.get("zip_code"),
                         watch.get("radius_miles"), watch.get("min_price"),
                         watch.get("max_price"), scan.get("id"), scan.get("status"),
                         scan.get("created_at"), scan.get("completed_at") or
                         scan.get("updated_at"), _duration_minutes(scan),
                         scan.get("cards_checked") or scan.get("scanned_count"),
                         scan.get("scanned_count"), alert_counts.get(scan.get("id"), 0),
                         scan.get("analysis_retry_pending_count"),
                         scan.get("error")))
    return output.getvalue()
