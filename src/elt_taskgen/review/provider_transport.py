"""Bounded HTTP transport and end-to-end wall deadlines for provider calls."""

from __future__ import annotations

import json
import signal
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from elt_taskgen.review.session import ProviderFault


# HTTP transport (injectable — tests double THIS, not the provider)

Transport = Callable[[str, Mapping[str, str], dict], dict]

_HTTP_TIMEOUT_SECONDS = 600.0

#: Transient statuses worth retrying: 429, generic 5xx, and Anthropic's 529.
#: Anything else — 400, 401, 403, 404 — is deterministic, and retrying only
#: re-bills the same failure.
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_HTTP_RETRIES = 4
_HTTP_BACKOFF_BASE_SECONDS = 2.0


class _OneShotWallDeadline(Exception):
    """Private signal escape; backends attach any completed attempts before
    re-raising it to the provider boundary."""


def _call_with_wall_deadline(
    call: Callable[[], Any], *, deadline_s: float | None, role_name: str
) -> Any:
    """Run one live one-shot model call under its declared wall deadline.

    ``urlopen``'s timeout is an inactivity timeout and can be reset forever by
    occasional response bytes.  The POSIX worker's main thread therefore uses
    an interval timer for the end-to-end call.  A threaded caller cannot
    enforce that safely and is refused before transport.
    """
    if deadline_s is None:
        return call()
    deadline = float(deadline_s)
    if deadline <= 0:
        raise ProviderFault(
            f"one-shot provider call for role {role_name!r} has no wall time remaining",
            code="wall_deadline_exceeded",
        )
    can_interrupt = (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not can_interrupt:
        raise ProviderFault(
            "one-shot model transport cannot enforce its wall deadline "
            "outside the worker process's main thread",
            code="wall_deadline_unsupported",
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def deadline_handler(_signum, _frame) -> None:
        raise _OneShotWallDeadline()

    signal.signal(signal.SIGALRM, deadline_handler)
    try:
        signal.setitimer(signal.ITIMER_REAL, deadline)
        try:
            return call()
        except _OneShotWallDeadline:
            raise ProviderFault(
                f"one-shot provider call for role {role_name!r} exceeded its "
                f"declared wall deadline of {deadline:g} s",
                code="wall_deadline_exceeded",
            ) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _http_post_json(url: str, headers: Mapping[str, str], payload: dict) -> dict:
    """POST canonical JSON, return the parsed JSON response (stdlib only).

    Transient statuses retry with exponential backoff (honouring Retry-After);
    the terminal attempt re-raises with the provider's own error detail.
    Determinism is unaffected — memoization happens above this layer.
    """
    body = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1 + _HTTP_RETRIES):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"content-type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=_HTTP_TIMEOUT_SECONDS
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - live API only
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            last_error = RuntimeError(
                f"provider HTTP {exc.code} from {url}: {detail}"
            )
            last_error.__cause__ = exc
            if exc.code not in _RETRYABLE_HTTP_STATUSES or attempt >= _HTTP_RETRIES:
                raise last_error
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0
            delay = max(delay, _HTTP_BACKOFF_BASE_SECONDS * (2**attempt))
            time.sleep(min(delay, 60.0))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:  # pragma: no cover
            # Connection-level transients get the same bounded courtesy; a
            # repeated failure still raises.
            last_error = RuntimeError(f"provider transport error from {url}: {exc}")
            last_error.__cause__ = exc
            if attempt >= _HTTP_RETRIES:
                raise last_error
            time.sleep(_HTTP_BACKOFF_BASE_SECONDS * (2**attempt))
    raise last_error  # pragma: no cover - loop always returns or raises
