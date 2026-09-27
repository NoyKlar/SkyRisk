"""In-process conversation memory for API sessions: idle TTL plus an LRU cap."""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from skyrisk.agent.core import Conversation


@dataclass
class Session:
    conversation: Conversation
    last_used: float
    lock: threading.Lock = field(default_factory=threading.Lock)  # serializes turns within a session


class SessionStore:
    def __init__(self, max_turns: int, *, ttl_s: float = 3600.0, max_sessions: int = 500,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._max_turns = max_turns
        self._ttl_s = ttl_s
        self._max_sessions = max_sessions
        self._now = now
        self._sessions: OrderedDict[str, Session] = OrderedDict()  # least recently used first
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None) -> tuple[str, Session, bool]:
        """The session for `session_id`, or a new one if it is missing or expired.

        The flag is True when a session id was given but no longer exists (memory was lost).
        """
        with self._lock:
            now = self._now()
            self._sweep(now)
            session = self._sessions.get(session_id) if session_id else None
            if session is not None:
                session.last_used = now
                self._sessions.move_to_end(session_id)
                return session_id, session, False
            new_id = secrets.token_urlsafe(16)
            session = Session(Conversation(max_turns=self._max_turns), last_used=now)
            self._sessions[new_id] = session
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
            return new_id, session, session_id is not None

    def reset(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        with session.lock:
            session.conversation.reset()
        return True

    def __len__(self) -> int:
        return len(self._sessions)

    def _sweep(self, now: float) -> None:
        while self._sessions:
            oldest_id, oldest = next(iter(self._sessions.items()))
            if now - oldest.last_used < self._ttl_s:
                return
            del self._sessions[oldest_id]
