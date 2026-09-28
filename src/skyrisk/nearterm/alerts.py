"""The alert check: recompute every hub's near-term score, compare it with the last snapshot, store
alerts and send one webhook message. Called by `POST /api/alerts/check` and `skyrisk alerts check`."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel

from skyrisk import db
from skyrisk.config import Level, NearTermConfig
from skyrisk.nearterm.engine import NearTermScore, apply_demo, score_forecast
from skyrisk.nearterm.service import NearTermService, NearTermUnavailable

Notify = Callable[[str], None]  # posts one message to the webhook; raises on failure
Log = Callable[[str], None]

UNITS = {"snowfall_cm": "cm", "wind_gust_max_kmh": "km/h", "precip_mm": "mm", "temp_max_c": "°C", "temp_min_c": "°C"}

_CHECK_LOCK = threading.Lock()  # one check at a time, so concurrent calls can't double-alert


class Alert(BaseModel):
    id: int
    created_at: str
    hub_id: str
    prev_score: float
    new_score: float
    prev_level: Level
    new_level: Level
    delta: float
    reason: str
    detail: str
    demo: bool
    webhook_status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Alert:
        return cls.model_validate(dict(row))


class CheckResult(BaseModel):
    checked_at: str
    demo_hub: str | None
    hubs_checked: list[str]
    baseline_only: list[str]  # hubs with no previous snapshot: this check only set their baseline
    errors: list[str]
    alerts: list[Alert]
    webhook_status: str  # sent | failed | skipped | none (no alerts)


def alert_reason(prev_score: float, prev_level: Level, new_score: float, new_level: Level,
                 cfg: NearTermConfig) -> str | None:
    """Why this change deserves an alert, or None. Increases and decreases both count."""
    parts = []
    if new_level != prev_level:
        parts.append(f"level {prev_level} -> {new_level}")
    delta = new_score - prev_score
    if abs(delta) >= cfg.alerts.change_threshold:
        parts.append(f"score changed by {delta:+.1f} (threshold {cfg.alerts.change_threshold:g})")
    return "; ".join(parts) or None


def driver(result: NearTermScore) -> str:
    """The hazard contributing most points, in words, e.g. 'snow peak 30 cm on 2026-09-29'."""
    top = max(result.hazards, key=lambda h: h.points)
    if top.points <= 0 or top.peak_value is None:
        return "no hazard above its watch threshold"
    return (f"{top.hazard.replace('_', ' ')} peak {top.peak_value:g} {UNITS.get(top.variable, '')} "
            f"on {top.peak_date.isoformat()}")


def format_message(alerts: list[Alert], registry_city: Callable[[str], str]) -> str:
    demo = any(a.demo for a in alerts)
    head = f"{'[DEMO] ' if demo else ''}:warning: SkyRisk near-term risk alert ({len(alerts)} hub{'s' if len(alerts) != 1 else ''})"
    lines = [
        f"• {registry_city(a.hub_id)}: {a.prev_score:.0f} → {a.new_score:.0f} ({a.prev_level} → {a.new_level}), {a.detail}"
        for a in alerts
    ]
    return "\n".join([head, *lines])


def run_check(conn: sqlite3.Connection, service: NearTermService, *, notify: Notify | None,
              demo_hub: str | None = None, log: Log = print, lock: threading.Lock = _CHECK_LOCK) -> CheckResult:
    """A normal check snapshots every hub; a demo check scores a scripted storm for one hub against
    its live score, records a `demo` alert and writes no snapshot, so the real baseline is untouched."""
    with lock:
        now = service.now()
        checked_at = now.isoformat(timespec="seconds")
        if demo_hub is not None:
            service.registry.get(demo_hub)  # KeyError for an unknown hub
            return _finish(conn, service, checked_at, [demo_hub], [], [], [_demo_alert(service, demo_hub)],
                           notify, log, demo_hub)

        cfg = service.cfg
        previous = db.latest_snapshots(conn)
        checked, baseline, errors, pending, snapshots = [], [], [], [], []
        for hub in service.registry.hubs:
            try:
                current = service.score(hub.id, fresh=True).result
            except NearTermUnavailable as e:
                errors.append(str(e))
                continue
            checked.append(hub.id)
            snapshots.append((hub.id, current.score, current.level))
            prev = previous.get(hub.id)
            if prev is None:
                baseline.append(hub.id)
                continue
            reason = alert_reason(prev["score"], prev["level"], current.score, current.level, cfg)
            if reason:
                pending.append(_alert_values(hub.id, prev["score"], prev["level"], current, reason, demo=False))
        db.insert_snapshots(conn, checked_at, snapshots, cfg.version)
        return _finish(conn, service, checked_at, checked, baseline, errors, pending, notify, log, None)


def _demo_alert(service: NearTermService, hub_id: str) -> dict[str, object]:
    live = service.score(hub_id, fresh=True)
    storm = score_forecast(apply_demo(live.days, service.cfg), service.cfg)
    reason = alert_reason(live.result.score, live.result.level, storm.score, storm.level, service.cfg)
    return _alert_values(hub_id, live.result.score, live.result.level, storm,
                         f"demo scenario; {reason}" if reason else "demo scenario", demo=True)


def _alert_values(hub_id: str, prev_score: float, prev_level: str, current: NearTermScore, reason: str, *,
                  demo: bool) -> dict[str, object]:
    return {
        "hub_id": hub_id, "prev_score": prev_score, "new_score": current.score, "prev_level": prev_level,
        "new_level": current.level, "delta": round(current.score - prev_score, 2), "reason": reason,
        "detail": driver(current), "demo": int(demo),
    }


def _finish(conn: sqlite3.Connection, service: NearTermService, checked_at: str, checked: list[str],
            baseline: list[str], errors: list[str], pending: list[dict[str, object]], notify: Notify | None,
            log: Log, demo_hub: str | None) -> CheckResult:
    status = "skipped" if notify is None else "pending"
    alerts = [Alert(id=db.insert_alert(conn, {**v, "created_at": checked_at, "webhook_status": status}),
                    created_at=checked_at, webhook_status=status, **v) for v in pending]
    conn.commit()
    if not alerts:
        status = "none"
    elif notify is not None:
        try:
            notify(format_message(alerts, lambda h: service.registry.get(h).city))
            status = "sent"
        except Exception as e:  # a broken webhook must never fail the check
            log(f"warning: alert webhook failed: {type(e).__name__}: {e}")
            status = "failed"
        db.set_webhook_status(conn, [a.id for a in alerts], status)
        alerts = [a.model_copy(update={"webhook_status": status}) for a in alerts]
    for e in errors:
        log(f"warning: near-term check: {e}")
    return CheckResult(checked_at=checked_at, demo_hub=demo_hub, hubs_checked=checked, baseline_only=baseline,
                       errors=errors, alerts=alerts, webhook_status=status)


def recent(conn: sqlite3.Connection, *, limit: int, hub_ids: list[str] | None = None,
           since: datetime | None = None) -> list[Alert]:
    rows = db.recent_alerts(conn, limit=limit, hub_ids=hub_ids,
                            since=since.isoformat(timespec="seconds") if since else None)
    return [Alert.from_row(r) for r in rows]
