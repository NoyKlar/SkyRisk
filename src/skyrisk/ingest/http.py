"""HTTP helpers with retry/backoff for external JSON APIs."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF_S = 60.0


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _with_retries(
    send: Callable[[], httpx.Response],
    *,
    retries: int,
    backoff_s: float,
    sleep: Callable[[float], None],
) -> dict:
    for attempt in range(retries + 1):
        delay = min(backoff_s * 2**attempt, MAX_BACKOFF_S)
        try:
            response = send()
        except httpx.TransportError:
            if attempt == retries:
                raise
        else:
            if response.status_code not in RETRY_STATUSES or attempt == retries:
                response.raise_for_status()
                return response.json()
            delay = _retry_after(response) or delay
        sleep(delay)
    raise AssertionError("unreachable")


def get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, object],
    *,
    retries: int = 6,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """GET a JSON document, retrying transient failures with exponential backoff.

    Honors a numeric Retry-After header. The default schedule (2, 4, ... capped at
    60 s) waits about 2 minutes in total, which outlasts per-minute API rate limits.
    """
    return _with_retries(lambda: client.get(url, params=params), retries=retries, backoff_s=backoff_s, sleep=sleep)


def post_json(
    client: httpx.Client,
    url: str,
    body: dict[str, object],
    headers: dict[str, str],
    *,
    retries: int = 6,
    backoff_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """POST a JSON body and return the JSON response, with the same retry policy as `get_json`."""
    return _with_retries(lambda: client.post(url, json=body, headers=headers),
                         retries=retries, backoff_s=backoff_s, sleep=sleep)
