"""Alert check (snapshots, rules, webhook, demo) and the alert API endpoints."""

import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes import FakeProvider
from skyrisk import db
from skyrisk.agent.core import Agent
from skyrisk.agent.tools import ToolContext
from skyrisk.api.app import create_app
from skyrisk.api.sessions import SessionStore
from skyrisk.config import load_scoring_config
from skyrisk.ingest import http
from skyrisk.nearterm.alerts import alert_reason, run_check
from test_near_term import CFG, REGISTRY, ROOT, Clock, fake_service, forecast

STORM = forecast({0: {"snowfall_cm": 15.0}})  # 70, high
RAIN = forecast({0: {"precip_mm": 50.0}})  # 35, medium


def _conn(check_same_thread=True):
    conn = db.connect(":memory:", check_same_thread=check_same_thread)
    db.upsert_hubs(conn, REGISTRY.hubs)
    return conn


class Scripted:
    """A mutable forecast script, so one service can see the weather change between checks."""

    def __init__(self):
        self.scripts = {}
        self.clock = Clock()
        self.service = fake_service(self.scripts, clock=self.clock)

    def set(self, hub_id, days):
        self.scripts[hub_id] = days
        self.clock.now += timedelta(hours=1)


def _check(conn, s, **kw):
    kw.setdefault("notify", None)
    return run_check(conn, s.service, log=lambda m: None, **kw)


def test_alert_reason_rules():
    assert alert_reason(10, "low", 25, "low", CFG) is None
    assert alert_reason(10, "low", 30, "low", CFG) == "score changed by +20.0 (threshold 20)"
    assert alert_reason(30, "low", 36, "medium", CFG) == "level low -> medium"
    assert alert_reason(90, "high", 20, "low", CFG) == "level high -> low; score changed by -70.0 (threshold 20)"


def test_first_check_only_sets_the_baseline():
    conn, s = _conn(), Scripted()
    s.set("houston", STORM)
    result = _check(conn, s)
    assert result.alerts == [] and result.webhook_status == "none"
    assert sorted(result.baseline_only) == sorted(h.id for h in REGISTRY.hubs)
    assert db.latest_snapshots(conn)["houston"]["score"] == 70.0


def test_changes_create_alerts_both_ways_and_small_changes_do_not():
    conn, s = _conn(), Scripted()
    _check(conn, s)
    s.set("houston", STORM)
    s.set("denver", forecast({0: {"snowfall_cm": 4.0}}))  # +10.8, still low: no alert
    result = _check(conn, s)
    assert [a.hub_id for a in result.alerts] == ["houston"] and result.baseline_only == []
    a = result.alerts[0]
    assert (a.prev_score, a.new_score, a.prev_level, a.new_level, a.delta) == (0.0, 70.0, "low", "high", 70.0)
    assert a.detail == "snow peak 15 cm on 2026-09-28" and a.demo is False and a.webhook_status == "skipped"

    s.set("houston", RAIN)  # 70 -> 35: a decrease and a level change
    down = _check(conn, s).alerts[0]
    assert (down.delta, down.new_level) == (-35.0, "medium") and "level high -> medium" in down.reason
    assert _check(conn, s).alerts == []  # unchanged


def test_failed_hub_keeps_its_old_snapshot_and_is_reported():
    conn, s = _conn(), Scripted()
    _check(conn, s)
    s.service._fetch = lambda hub: (_ for _ in ()).throw(httpx.ConnectError("down")) if hub.id == "miami" \
        else forecast()
    before = db.latest_snapshots(conn)["miami"]["checked_at"]
    s.clock.now += timedelta(hours=1)
    result = _check(conn, s)
    assert "miami" not in result.hubs_checked and any("miami" in e for e in result.errors)
    assert db.latest_snapshots(conn)["miami"]["checked_at"] == before


def _webhook(bodies, status=200):
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(status, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return lambda text: http.post(client, "https://hooks.example/x", {"text": text}, sleep=lambda s: None)


def test_webhook_gets_one_slack_message_per_check():
    conn, s, bodies = _conn(), Scripted(), []
    _check(conn, s)
    s.set("houston", STORM)
    s.set("minneapolis", RAIN)
    result = _check(conn, s, notify=_webhook(bodies))
    assert result.webhook_status == "sent" and len(bodies) == 1
    text = bodies[0]["text"]
    assert text.startswith(":warning: SkyRisk near-term risk alert (2 hubs)")
    assert "• Houston: 0 → 70 (low → high), snow peak 15 cm on 2026-09-28" in text
    assert all(a["webhook_status"] == "sent" for a in conn.execute("SELECT webhook_status FROM alerts"))


def test_webhook_failure_is_recorded_and_never_fails_the_check():
    conn, s, bodies, logs = _conn(), Scripted(), [], []
    _check(conn, s)
    s.set("houston", STORM)
    result = run_check(conn, s.service, notify=_webhook(bodies, status=500), log=logs.append)
    assert result.webhook_status == "failed" and result.alerts[0].webhook_status == "failed"
    assert len(bodies) == 3 and any("webhook failed" in m for m in logs)  # 1 try + 2 retries


def test_demo_alerts_without_touching_the_baseline():
    conn, s, bodies = _conn(), Scripted(), []
    _check(conn, s)
    snapshots = conn.execute("SELECT COUNT(*) FROM near_term_snapshots").fetchone()[0]
    result = _check(conn, s, notify=_webhook(bodies), demo_hub="chicago")
    a = result.alerts[0]
    assert result.demo_hub == "chicago" and result.hubs_checked == ["chicago"]
    assert a.demo is True and a.prev_score == 0.0 and a.new_level == "high" and a.reason.startswith("demo scenario")
    assert bodies[0]["text"].startswith("[DEMO] ")
    assert conn.execute("SELECT COUNT(*) FROM near_term_snapshots").fetchone()[0] == snapshots
    assert _check(conn, s).alerts == []  # the next real check sees no change


def test_demo_works_before_any_baseline():
    conn, s = _conn(), Scripted()
    result = _check(conn, s, demo_hub="denver")
    assert len(result.alerts) == 1 and db.latest_snapshots(conn) == {}


def test_unknown_demo_hub():
    with pytest.raises(KeyError):
        _check(_conn(), Scripted(), demo_hub="seattle")


# --- API ----------------------------------------------------------------------------------

@pytest.fixture
def api():
    conn = _conn(check_same_thread=False)
    s = Scripted()
    ctx = ToolContext(conn, REGISTRY, load_scoring_config(ROOT / "config" / "scoring.yaml"), s.service)

    def make(token="secret", **kw):
        agent = Agent(ctx, [FakeProvider()], "system", log=lambda m: None)
        app = create_app(agent, SessionStore(max_turns=10), ctx, log=lambda m: None, alert_token=token, **kw)
        return TestClient(app)

    yield make, s
    conn.close()


AUTH = {"Authorization": "Bearer secret"}


def test_check_is_disabled_without_a_token(api):
    make, _ = api
    res = make(token=None).post("/api/alerts/check", headers=AUTH)
    assert res.status_code == 503 and "ALERT_TOKEN" in res.json()["detail"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "secret"},
                                     {"Authorization": "Basic secret"}])
def test_check_rejects_bad_or_missing_token(api, headers):
    make, _ = api
    assert make().post("/api/alerts/check", headers=headers).status_code == 401


def test_check_then_list_alerts(api):
    make, s = api
    client = make()
    first = client.post("/api/alerts/check", headers=AUTH)
    assert first.status_code == 200 and len(first.json()["baseline_only"]) == len(REGISTRY.hubs)
    s.set("houston", STORM)
    second = client.post("/api/alerts/check", headers=AUTH, json={}).json()
    assert [a["hub_id"] for a in second["alerts"]] == ["houston"]
    demo = client.post("/api/alerts/check", headers=AUTH, json={"demo_hub": "chicago"}).json()
    assert demo["alerts"][0]["demo"] is True

    listed = client.get("/api/alerts").json()
    assert [(a["city"], a["demo"]) for a in listed["alerts"]] == [("Chicago", True), ("Houston", False)]
    assert listed["last_check_at"] == second["checked_at"]
    assert [a["hub_id"] for a in client.get("/api/alerts?hub_id=houston").json()["alerts"]] == ["houston"]
    assert len(client.get("/api/alerts?limit=1").json()["alerts"]) == 1


def test_list_limits_and_unknown_demo_hub(api):
    make, _ = api
    client = make()
    assert client.get("/api/alerts?limit=51").status_code == 422
    assert client.get("/api/alerts").json() == {"alerts": [], "last_check_at": None}
    res = client.post("/api/alerts/check", headers=AUTH, json={"demo_hub": "seattle"})
    assert res.status_code == 400


def test_check_fails_when_no_forecast_can_be_fetched(api):
    make, s = api
    s.service._fetch = lambda hub: (_ for _ in ()).throw(httpx.ConnectError("down"))
    res = make().post("/api/alerts/check", headers=AUTH)
    assert res.status_code == 502 and res.json()["errors"]


def test_near_term_levels_endpoint(api):
    make, s = api
    s.set("houston", STORM)
    body = make().get("/api/near-term").json()
    levels = {h["hub_id"]: h["level"] for h in body["hubs"]}
    assert len(levels) == len(REGISTRY.hubs) and levels["houston"] == "high" and levels["denver"] == "low"
    assert body["cache_ttl_s"] == CFG.cache_ttl_s
