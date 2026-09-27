"""FastAPI app: the chat page at `/` and the agent's JSON API under `/api`."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from skyrisk.agent.core import Agent, Log, Status
from skyrisk.agent.tools import HubInfo, ListHubsInput, ToolContext, list_hubs
from skyrisk.api.sessions import SessionStore

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


def create_app(agent: Agent, sessions: SessionStore, ctx: ToolContext, *, log: Log = print) -> FastAPI:
    app = FastAPI(title="SkyRisk", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log(f"error: {request.method} {request.url.path} failed: {type(exc).__name__}: {exc}")
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> ChatResponse:
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

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
