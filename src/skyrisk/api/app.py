"""FastAPI app: the chat page at `/` and the agent's JSON API under `/api`."""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from skyrisk import db
from skyrisk.agent.core import Agent, Log, Status
from skyrisk.agent.tools import HubInfo, ListHubsInput, ToolContext, list_hubs
from skyrisk.api.ratelimit import RateLimiter
from skyrisk.api.sessions import SessionStore
from skyrisk.nearterm import alerts as nt_alerts
from skyrisk.nearterm.alerts import Alert, CheckResult, Notify
from skyrisk.nearterm.service import HubLevel

STATIC_DIR = Path(__file__).parent / "static"
MAX_MESSAGE_CHARS = 10_000  # transport cap; the agent's own limit produces the friendly message


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    session_id: str
    session_reset: bool = Field(description="True when the given session had expired and a new one was started")
    status: Status
    answer: str
    limitations: list[str]
    hubs: list[str]
    scores_cited: list[dict]
    served_by: str | None
    tools_used: list[str]


class AlertCheckRequest(BaseModel):
    demo_hub: str | None = Field(default=None, description="Simulate a storm for this hub (demo alert, no baseline change)")


class AlertView(Alert):
    city: str


class AlertsResponse(BaseModel):
    alerts: list[AlertView]
    last_check_at: str | None = Field(description="When the last (non-demo) check ran; None since the last restart")


class NearTermLevels(BaseModel):
    hubs: list[HubLevel]
    cache_ttl_s: float = Field(description="Forecasts are reused for this long, so levels can be up to this old")


def client_ip(request: Request) -> str:
    """The caller's IP: the first X-Forwarded-For entry (set by Render's proxy), else the socket peer.

    The header can be spoofed, so per-IP limits are for fairness only; the global cap bounds spend.
    """
    if forwarded := request.headers.get("x-forwarded-for"):
        if first := forwarded.split(",")[0].strip():
            return first
    return request.client.host if request.client else "unknown"


def create_app(agent: Agent, sessions: SessionStore, ctx: ToolContext, *, log: Log = print,
               limiter: RateLimiter | None = None, alert_token: str | None = None,
               notify: Notify | None = None) -> FastAPI:
    """`limiter=None` disables rate limiting; `skyrisk serve` always passes one.

    `alert_token` guards `POST /api/alerts/check`; without it (or without `ctx.near_term`) the check is
    disabled. `notify` posts one message to the alert webhook; None skips the webhook.
    """
    app = FastAPI(title="SkyRisk", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log(f"error: {request.method} {request.url.path} failed: {type(exc).__name__}: {exc}")
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    @app.post("/api/chat", responses={429: {"description": "Rate limit reached"}})
    def chat(req: ChatRequest, request: Request) -> ChatResponse:  # or a 429 JSONResponse
        if limiter is not None:
            ip = client_ip(request)
            if limited := limiter.check(ip):
                log(f"rate limit ({limited.scope}) for {ip}")
                return JSONResponse({"detail": limited.message}, status_code=429,
                                    headers={"Retry-After": str(limited.retry_after)})
        session_id, session, was_reset = sessions.get_or_create(req.session_id)
        with session.lock:
            reply = agent.ask(session.conversation, req.message)
        # Guardrail and provider details stay in the server log, never in the response.
        if reply.guardrail:
            log(f"session {session_id[:8]}: guardrail {reply.guardrail}")
        if reply.warnings:
            log(f"session {session_id[:8]}: warnings: {'; '.join(reply.warnings)}")
        return ChatResponse(
            session_id=session_id,
            session_reset=was_reset,
            status=reply.status,
            answer=reply.text,
            limitations=reply.limitations,
            hubs=reply.hubs,
            scores_cited=reply.scores_cited,
            served_by=reply.served_by,
            tools_used=list(dict.fromkeys(reply.tools_used)),
        )

    @app.delete("/api/sessions/{session_id}", status_code=204)
    def reset_session(session_id: str) -> Response:
        sessions.reset(session_id)
        return Response(status_code=204)

    @app.get("/api/hubs")
    def hubs() -> list[HubInfo]:
        return list_hubs(ctx, ListHubsInput()).hubs

    @app.post("/api/alerts/check", responses={
        401: {"description": "Missing or wrong token"}, 503: {"description": "Alert checks are disabled"}})
    def check_alerts(req: AlertCheckRequest | None = None,
                     authorization: str | None = Header(default=None)) -> CheckResult:  # or an error JSONResponse
        if not alert_token or ctx.near_term is None:
            return JSONResponse({"detail": "Alert checks are disabled on this server (ALERT_TOKEN is not set)."},
                                status_code=503)
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), alert_token.encode()):
            log("alerts check: rejected (bad or missing token)")
            return JSONResponse({"detail": "Invalid or missing token."}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        demo_hub = req.demo_hub if req else None
        if demo_hub is not None and demo_hub not in {h.id for h in ctx.registry.hubs}:
            return JSONResponse({"detail": f"Unknown hub id {demo_hub!r}."}, status_code=400)
        result = nt_alerts.run_check(ctx.conn, ctx.near_term, notify=notify, demo_hub=demo_hub, log=log)
        log(f"alerts check{' (demo ' + demo_hub + ')' if demo_hub else ''}: {len(result.hubs_checked)} hubs, "
            f"{len(result.baseline_only)} baseline only, {len(result.alerts)} alerts, webhook {result.webhook_status}")
        if not result.hubs_checked:
            return JSONResponse({"detail": "No forecast could be fetched.", "errors": result.errors}, status_code=502)
        return result

    @app.get("/api/alerts")
    def recent_alerts(limit: int = Query(default=20, ge=1, le=50), hub_id: str | None = None) -> AlertsResponse:
        found = nt_alerts.recent(ctx.conn, limit=limit, hub_ids=[hub_id] if hub_id else None)
        cities = {h.id: h.city for h in ctx.registry.hubs}
        return AlertsResponse(
            alerts=[AlertView(**a.model_dump(), city=cities.get(a.hub_id, a.hub_id)) for a in found],
            last_check_at=db.last_check_at(ctx.conn),
        )

    @app.get("/api/near-term", responses={503: {"description": "The near-term forecast is not configured"}})
    def near_term_levels() -> NearTermLevels:  # or an error JSONResponse
        # Public: the 1 h forecast cache bounds outbound calls to about one per hub per hour.
        if ctx.near_term is None:
            return JSONResponse({"detail": "The near-term forecast is not configured on this server."},
                                status_code=503)
        return NearTermLevels(hubs=ctx.near_term.levels(), cache_ttl_s=ctx.near_term.cfg.cache_ttl_s)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
