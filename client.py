"""Small async client for the Chatwork REST API v2 (https://developer.chatwork.com).

Only what the adapter needs. It owns the rate-limit discipline so callers do
not have to: Chatwork allows 300 requests per 5 minutes per token (reported in
``x-ratelimit-*`` headers) and 10 posts per 10 seconds per room. The client
slows down before the budget runs out, waits out a 429 and retries, and turns
every failure into an exception whose message says what to do next.

The token is sent only in the ``x-chatworktoken`` header and never appears in
an exception message or log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.chatwork.com/v2"
TOKEN_PAGE = "https://www.chatwork.com/service/packages/chatwork/subpackages/api/token.php"

# Keep this many calls in reserve; below it, wait for the window to reset
# instead of spending the last calls (sending replies matters more than polling).
RATE_RESERVE = 10
MAX_RATE_WAIT = 300.0  # one full Chatwork window
ROOM_POST_WAIT = 10.0  # Chatwork's advice for the per-room posting limit
MAX_429_RETRIES = 3
# Replies never sleep longer than this inline (same cap as Hermes' own send
# retry); a longer wait is handed back to the gateway as retry_after.
SEND_INLINE_WAIT_CAP = 60.0


class ChatworkError(Exception):
    """Base error. ``str(exc)`` is safe to log and to show to the operator."""

    retryable = True


class ChatworkAuthError(ChatworkError):
    """401: the token is wrong, revoked or missing. Retrying will not help."""

    retryable = False


class ChatworkAccessError(ChatworkError):
    """403/404: the account cannot see or post in that room."""

    retryable = False

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class ChatworkRateLimited(ChatworkError):
    """429 that persisted through our retries. ``wait`` is the advised pause."""

    def __init__(self, message: str, wait: float):
        super().__init__(message)
        self.wait = wait


class ChatworkTransientError(ChatworkError):
    """Network trouble or a 5xx. Back off and try again."""


def _errors_text(resp: httpx.Response) -> str:
    try:
        errs = resp.json().get("errors")
        if isinstance(errs, list) and errs:
            return "; ".join(str(e) for e in errs)[:300]
    except Exception:
        pass
    return (resp.text or "").strip()[:300]


class ChatworkClient:
    """Thin wrapper over ``httpx.AsyncClient`` with Chatwork's rate rules."""

    def __init__(self, token: str, *, base_url: str = API_BASE,
                 http: Optional[httpx.AsyncClient] = None, sleep=asyncio.sleep,
                 clock=time.time, timeout: float = 20.0):
        self._token = (token or "").strip()
        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None
        self._sleep = sleep
        self._clock = clock
        self.remaining: Optional[int] = None
        self.reset_at: Optional[float] = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- rate budget --------------------------------------------------------

    def _note_limits(self, resp: httpx.Response) -> None:
        try:
            if "x-ratelimit-remaining" in resp.headers:
                self.remaining = int(resp.headers["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in resp.headers:
                self.reset_at = float(resp.headers["x-ratelimit-reset"])
        except (TypeError, ValueError):
            pass

    def _seconds_to_reset(self) -> float:
        if self.reset_at is None:
            return 0.0
        return max(0.0, min(MAX_RATE_WAIT, self.reset_at - self._clock() + 1.0))

    async def _respect_budget(self, *, priority: bool, max_wait: Optional[float]) -> None:
        """Wait for the window to reset when the budget is nearly spent.

        Replies (``priority``) may use the reserve; polling may not.
        """
        if self.remaining is None:
            return
        floor = 0 if priority else RATE_RESERVE
        if self.remaining <= floor:
            wait = self._seconds_to_reset()
            if wait > 0:
                if max_wait is not None and wait > max_wait:
                    raise ChatworkRateLimited(
                        f"Chatwork API budget for this 5-minute window is used up; it resets in {wait:.0f}s.", wait)
                logger.info("Chatwork: %d API calls left in this 5-minute window; waiting %.0fs for it to reset.",
                            self.remaining, wait)
                await self._sleep(wait)
                self.remaining = None

    # -- core request ------------------------------------------------------

    async def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                      data: Optional[Dict[str, Any]] = None, priority: bool = False,
                      max_wait: Optional[float] = None) -> Any:
        """Call the API and return parsed JSON (``None`` for 204 No Content).

        ``max_wait`` caps any single rate-limit pause; a longer one raises
        :class:`ChatworkRateLimited` instead of sleeping.
        """
        if not self._token:
            raise ChatworkAuthError(
                "CHATWORK_API_TOKEN is not set. Create a token at " + TOKEN_PAGE
                + " (log in as the bot account), then add CHATWORK_API_TOKEN=... to ~/.hermes/.env.")
        url = f"{self._base}/{path.lstrip('/')}"
        headers = {"x-chatworktoken": self._token, "Accept": "application/json"}
        attempt = 0
        while True:
            await self._respect_budget(priority=priority, max_wait=max_wait)
            try:
                resp = await self._http.request(method, url, params=params, data=data, headers=headers)
            except httpx.TimeoutException:
                raise ChatworkTransientError(f"Chatwork did not answer {method} /{path} in time; will retry.")
            except httpx.HTTPError as exc:
                raise ChatworkTransientError(
                    f"Could not reach api.chatwork.com ({type(exc).__name__}); check the network or proxy. Will retry.")
            self._note_limits(resp)
            status = resp.status_code
            if status == 204:
                return None
            if 200 <= status < 300:
                try:
                    return resp.json()
                except ValueError:
                    raise ChatworkTransientError(f"Chatwork returned a non-JSON body for {method} /{path}; will retry.")
            if status == 429:
                detail = _errors_text(resp)
                per_room = "per room" in detail.lower()
                wait = ROOM_POST_WAIT if per_room else (self._seconds_to_reset() or 2.0 * (2 ** attempt))
                if attempt >= MAX_429_RETRIES or (max_wait is not None and wait > max_wait):
                    raise ChatworkRateLimited(
                        "Chatwork rate limit still exceeded after retries "
                        f"({'10 posts per 10s in this room' if per_room else '300 calls per 5 min'}). "
                        "If this keeps happening, raise CHATWORK_POLL_INTERVAL or watch fewer rooms.", wait)
                attempt += 1
                logger.warning("Chatwork: rate limited (%s); waiting %.0fs, then retry %d/%d.",
                               "per-room posting" if per_room else "API calls", wait, attempt, MAX_429_RETRIES)
                await self._sleep(wait)
                self.remaining = None
                continue
            detail = _errors_text(resp)
            if status == 401:
                raise ChatworkAuthError(
                    f"Chatwork rejected the API token (401: {detail}). The token was mistyped, regenerated "
                    f"or belongs to a deleted account. Issue a new one at {TOKEN_PAGE} while logged in as the "
                    "bot account, put it in CHATWORK_API_TOKEN, and restart the gateway.")
            if status in (403, 404):
                raise ChatworkAccessError(f"Chatwork returned {status} for {method} /{path}: {detail}", status)
            if status >= 500:
                raise ChatworkTransientError(f"Chatwork server error {status} on {method} /{path}; will retry.")
            raise ChatworkError(f"Chatwork returned {status} for {method} /{path}: {detail}")

    # -- endpoints ---------------------------------------------------------

    async def me(self) -> Dict[str, Any]:
        return await self.request("GET", "me")

    async def rooms(self) -> List[Dict[str, Any]]:
        return await self.request("GET", "rooms") or []

    async def messages(self, room_id: str) -> List[Dict[str, Any]]:
        """The latest (up to 100) messages. ``force=1`` so the server-side
        "unread" pointer, shared by everything using this token, is not used."""
        return await self.request("GET", f"rooms/{int(room_id)}/messages", params={"force": 1}) or []

    async def message(self, room_id: str, message_id: str, *, max_wait: Optional[float] = None) -> Dict[str, Any]:
        return await self.request("GET", f"rooms/{int(room_id)}/messages/{int(message_id)}", max_wait=max_wait)

    async def post_message(self, room_id: str, body: str) -> str:
        data = await self.request("POST", f"rooms/{int(room_id)}/messages", data={"body": body},
                                  priority=True, max_wait=SEND_INLINE_WAIT_CAP)
        return str((data or {}).get("message_id") or "")

    async def update_message(self, room_id: str, message_id: str, body: str) -> str:
        data = await self.request("PUT", f"rooms/{int(room_id)}/messages/{int(message_id)}",
                                  data={"body": body}, priority=True, max_wait=SEND_INLINE_WAIT_CAP)
        return str((data or {}).get("message_id") or message_id)
