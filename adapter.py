"""Chatwork gateway adapter for Hermes Agent.

Talk to Hermes from a Chatwork room the same way you would from Telegram,
Slack or Discord. No public URL is needed: the adapter polls the Chatwork API
(within its 300-calls-per-5-minutes limit) and answers with a normal Chatwork
reply, so the person who asked gets the usual notification.

Settings (environment, or ``platforms.chatwork.extra`` in config.yaml):

    CHATWORK_API_TOKEN            API token of the account Hermes speaks as (required)
    CHATWORK_ROOMS                Room ids to watch, comma-separated (required to answer anything)
    CHATWORK_REQUIRE_MENTION      true (default): in group rooms answer only [To:bot] or a reply to the bot
    CHATWORK_FREE_RESPONSE_ROOMS  Room ids where every message is answered, no [To:] needed
    CHATWORK_POLL_INTERVAL        Seconds between checks (default 5, minimum 2)
    CHATWORK_HOME_CHANNEL         Room id for cron / `hermes send` deliveries and Hermes' own notices (no default)
    CHATWORK_ALLOWED_USERS        Optional: only these account ids may use the bot. While it is unset,
                                  Hermes commands other than /help, /whoami, /new and /reset, and
                                  approval answers, are kept to the account the token belongs to
    CHATWORK_NOTICE_DELIVERY      private (default): Hermes' per-person notices are not posted in group rooms
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

try:  # documented helpers (Hermes >= 0.21.1); fall back to the process env on older builds
    from gateway.platforms._shared import extra_or_secret as _extra_or_secret
    from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
except ImportError:  # pragma: no cover - exercised only on old Hermes builds
    import os

    def _get_scoped_secret(name: str, default: Any = None, **_: Any) -> Any:
        return os.environ.get(name, default)

    def _extra_or_secret(extra: Optional[dict], key: str, env: str, default: Any = "", **_: Any) -> Any:
        val = os.environ.get(env, "").strip()
        if val:
            return val
        if extra and extra.get(key) not in (None, ""):
            return extra[key]
        return default

if __package__:
    from .client import ChatworkAccessError, ChatworkAuthError, ChatworkClient, ChatworkError, \
        ChatworkRateLimited, ChatworkTransientError, TOKEN_PAGE
    from .markup import parse_inbound, quote_preview, render_outbound, reply_header
    from .state import CursorState
else:  # flat import only when loaded as a top-level module (pytest rootdir, validator probe)
    from client import ChatworkAccessError, ChatworkAuthError, ChatworkClient, ChatworkError, \
        ChatworkRateLimited, ChatworkTransientError, TOKEN_PAGE  # type: ignore
    from markup import parse_inbound, quote_preview, render_outbound, reply_header  # type: ignore
    from state import CursorState  # type: ignore

logger = logging.getLogger(__name__)

PLATFORM_NAME = "chatwork"
PLUGIN_NAME = "jp-chatwork"
# Chatwork accepts up to 65,535 characters; longer answers are split here so
# each message stays readable and copyable.
MAX_MESSAGE_LENGTH = 10000
DEFAULT_POLL_INTERVAL = 5.0
MIN_POLL_INTERVAL = 2.0
FULL_REFRESH_EVERY = 12  # also re-read rooms whose metadata did not change, every N polls
MAX_BACKOFF = 120.0
# After downtime, questions up to this old are still answered; older ones are
# skipped so nobody gets a surprise answer to last week's question.
MAX_CATCHUP_SECONDS = 24 * 3600
REPLY_CONTEXT_MAX_WAIT = 5.0  # seconds; past this the question goes on without the replied-to text
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

PLATFORM_HINT = (
    "You are answering in Chatwork, a business chat app used mostly by Japanese companies; "
    "the people writing to you are usually colleagues in a small or mid-sized company. "
    "Chatwork does not render Markdown: write plain text. Do not use **bold**, # headings or tables. "
    "Use short paragraphs and simple lists starting with '・'. Put code, commands or log output in a "
    "fenced code block (```); it is shown as a Chatwork code box. Reply in the language the person "
    "used (usually Japanese, polite です・ます). The reply is addressed to the asker automatically, "
    "so never write [To:...], [rp ...] or [toall] yourself."
)

# While CHATWORK_ALLOWED_USERS is unset, everyone in a listed room may talk to
# Hermes, but Hermes' own commands and approval answers stay with the account
# the token belongs to. Others keep only these harmless, self-scoped commands.
OPEN_COMMANDS = frozenset({"help", "whoami", "new", "reset"})
# Bare words Hermes accepts as an answer while an approval is waiting.
# English lists match locales/en.yaml ``approval.inputs.*``; Japanese defaults from
# locales/ja.yaml are included so fail-closed still refuses 「はい」 when
# ``approval_input_words`` cannot load the active language pack.
_APPROVAL_WORDS_0214 = frozenset({
    # en: approve / deny / always / session (+ thumbs Hermes always matches)
    "approve", "yes", "ok", "okay", "confirm", "y", "👍",
    "deny", "no", "reject", "cancel", "n", "👎",
    "always", "approve always", "always approve",
    "session", "approve session", "session approve",
    # ja defaults (approval.inputs.approve/deny/always/session)
    "承認", "はい", "了解", "許可", "実行", "確認",
    "拒否", "いいえ", "却下", "キャンセル", "だめ",
    "常に", "常に承認", "いつも承認",
    "セッション", "セッション承認", "セッション中は承認",
})
# Bare words that answer "always" to a pending /new or /reset confirmation,
# which turns those confirmations off for the whole profile.
_CONFIRM_ALWAYS_WORDS_0214 = frozenset({
    "always", "always approve", "remember",
    "常に", "常に承認", "記憶",
})
COMMAND_REFUSAL = (
    "Only the account this bot's API token belongs to can use Hermes commands (/approve, /sethome, /pause, "
    "/model, /memory and so on) or answer \"yes\" while an approval is waiting, because CHATWORK_ALLOWED_USERS "
    "is not set. Everyone else can use /help, /whoami, /new and /reset. To let others in, whoever runs Hermes "
    "adds CHATWORK_ALLOWED_USERS=<account id>,<account id> to ~/.hermes/.env and restarts; this also limits "
    "who can talk to the bot. An approval nobody answers expires and the action does not run.\n\n"
    "Hermes のコマンド（/approve・/sethome・/pause・/model・/memory など）と、承認待ちの間の「はい」「yes」には、"
    "このボットの API トークンのアカウントしか答えられません。CHATWORK_ALLOWED_USERS が設定されていないためです。"
    "ほかの人が使えるコマンドは /help・/whoami・/new・/reset だけです。"
    "ほかの人にも任せるには、Hermes を動かしている人が ~/.hermes/.env に "
    "CHATWORK_ALLOWED_USERS=アカウントID,アカウントID を書いて再起動します（AI と話せる人もその人たちに絞られます）。"
    "誰も答えなかった承認は時間切れになり、その操作は実行されません。"
)


# --- config helpers ----------------------------------------------------------

def _csv_ids(raw: Any) -> List[str]:
    """'123, 456' / [123, '456'] -> ['123', '456'] (digits only, order kept)."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw).replace(";", ",").split(",")
    out: List[str] = []
    for item in items:
        s = str(item).strip()
        if s.lower().startswith("rid"):  # people paste "rid123456" from the room URL
            s = s[3:]
        if s.isdigit() and s not in out:
            out.append(s)
    return out


def _bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw if raw is not None else "").strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return default


def _float(raw: Any, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _token_from(extra: Optional[dict]) -> str:
    return str(_extra_or_secret(extra or {}, "token", "CHATWORK_API_TOKEN", "") or "").strip()


def _allowed_users_set() -> bool:
    """Is CHATWORK_ALLOWED_USERS set? Read the way the gateway's own allowlist check reads it."""
    try:
        from gateway.platforms._shared import platform_gate_env
        raw = platform_gate_env("CHATWORK_ALLOWED_USERS")
    except ImportError:  # pragma: no cover - very old Hermes
        import os
        raw = os.environ.get("CHATWORK_ALLOWED_USERS", "")
    return bool(str(raw or "").strip())


def _input_words(fixed: frozenset, keys: Tuple[str, ...]) -> frozenset:
    """Hermes' typed-reply words for ``approval.inputs.<key>`` (fixed defaults ∪ active language).

    ``fixed`` already carries English + Japanese defaults so reserved-word detection
    stays closed if ``approval_input_words`` fails. Active-language extras are additive.
    """
    words = set(fixed)
    try:
        from gateway.run_busy import approval_input_words
        for key in keys:
            words.update(approval_input_words(key))
    except Exception:
        # Fail closed: keep the fixed English+Japanese defaults; do not shrink the set.
        pass
    return frozenset(words)


def _state_path() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_NAME) / "state.json"
    except Exception:  # older builds: same layout, resolved by hand
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home()) / "plugin-data" / PLUGIN_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root / "state.json"


def _chat_type(room_type: str) -> str:
    # Chatwork room types: "my" (My Chat), "direct" (1:1), "group".
    return "dm" if room_type in ("my", "direct") else "group"


# --- adapter -----------------------------------------------------------------

class ChatworkAdapter(BasePlatformAdapter):
    """Chatwork rooms <-> Hermes, by API polling."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    splits_long_messages = True  # send() splits via truncate_message()
    # Token-by-token streaming would be one API call per edit and would strip
    # the reply header; Chatwork gets the finished answer as one message.
    # (Tool-progress bubbles still use edit_message below.)
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig, *, client: Optional[ChatworkClient] = None,
                 state_path: Optional[Path] = None):
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = config.extra or {}
        self._token = _token_from(extra)
        self._rooms: List[str] = _csv_ids(_extra_or_secret(extra, "rooms", "CHATWORK_ROOMS", ""))
        self._free_rooms = set(_csv_ids(_extra_or_secret(extra, "free_response_rooms", "CHATWORK_FREE_RESPONSE_ROOMS", "")))
        self._require_mention = _bool(_extra_or_secret(extra, "require_mention", "CHATWORK_REQUIRE_MENTION", "true"), True)
        self._poll_interval = _float(_extra_or_secret(extra, "poll_interval", "CHATWORK_POLL_INTERVAL", DEFAULT_POLL_INTERVAL),
                                     DEFAULT_POLL_INTERVAL, MIN_POLL_INTERVAL)
        self._client = client
        self._owns_client = client is None
        self._state_path = state_path
        self._state: Optional[CursorState] = None
        self._bot_id = ""
        self._bot_name = ""
        self._room_meta: Dict[str, Dict[str, Any]] = {}
        self._room_seen: Dict[str, Tuple[Any, Any]] = {}
        self._warned_rooms: set = set()
        self._inbound: "OrderedDict[str, Tuple[str, str, str]]" = OrderedDict()  # msg id -> (room, aid, name)
        self._addressed: set = set()  # inbound ids whose answer already carried the reply header
        self._headers: "OrderedDict[str, str]" = OrderedDict()  # sent msg id -> reply header it carries
        self._io_lock = asyncio.Lock()
        self._poll_task: Optional[asyncio.Task] = None
        self._polls = 0
        self._token_lock_held = False
        self._sleep = asyncio.sleep  # swapped in tests

    # Access policy: the room allowlist (CHATWORK_ROOMS) is enforced here, at
    # intake, so everyone in a listed room may talk to the bot. Set
    # CHATWORK_ALLOWED_USERS to narrow it to specific accounts. While it is
    # unset, Hermes commands and approval answers from anyone but the token's
    # account are stopped here too (see _refuse_for_guest).
    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    _dm_policy = "allowlist"
    _group_policy = "allowlist"

    # -- lifecycle --------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._token:
            msg = ("CHATWORK_API_TOKEN is not set. Log in to Chatwork as the bot account, create a token at "
                   f"{TOKEN_PAGE}, add CHATWORK_API_TOKEN=... to ~/.hermes/.env and restart the gateway.")
            logger.error("[%s] %s", self.name, msg)
            self._set_fatal_error("chatwork_no_token", msg, retryable=False)
            return False
        if not self._acquire_token_lock():
            return False
        if self._client is None:
            self._client = ChatworkClient(self._token)
        try:
            me = await self._client.me()
            self._bot_id = str(me.get("account_id") or "")
            self._bot_name = str(me.get("name") or "")
            await self._refresh_rooms(log_setup=True)
        except ChatworkAuthError as exc:
            logger.error("[%s] %s", self.name, exc)
            self._set_fatal_error("chatwork_unauthorized", str(exc), retryable=False)
            await self._close_client()
            self._release_token_lock()
            return False
        except ChatworkError as exc:
            logger.error("[%s] Could not start: %s", self.name, exc)
            self._set_fatal_error("chatwork_connect_failed", str(exc), retryable=True)
            await self._close_client()
            self._release_token_lock()
            return False

        self._state = CursorState(self._state_path or _state_path()).load()
        if self._state.account_id and self._state.account_id != self._bot_id:
            logger.warning("[%s] The token now belongs to a different Chatwork account; starting from the newest "
                           "messages in every room.", self.name)
            self._state = CursorState(self._state.path)
        self._state.account_id = self._bot_id
        # Forget the read position of rooms no longer listed: if one is added
        # back later, Hermes starts from that moment instead of answering what
        # was said while the room was not allowed.
        for rid in [r for r in self._state.cursors if r not in self._rooms]:
            del self._state.cursors[rid]
        self._save_state()

        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[%s] Connected as %s (account %s). Watching %d room(s), checking every %.0fs.",
                    self.name, self._bot_name or "?", self._bot_id, len(self._watched_rooms()), self._poll_interval)
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        self._save_state()
        await self._close_client()
        self._release_token_lock()
        self._mark_disconnected()

    async def _close_client(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _acquire_token_lock(self) -> bool:
        """One poller per token on this machine; two would answer every message twice."""
        acquire = getattr(self, "_acquire_platform_lock", None)
        if acquire is None:  # pragma: no cover - very old Hermes
            return True
        if not acquire(PLATFORM_NAME, self._token, "This Chatwork API token"):
            return False  # the helper logged who holds it and set a retryable fatal error
        self._token_lock_held = True
        return True

    def _release_token_lock(self) -> None:
        if not self._token_lock_held:
            return
        try:
            self._release_platform_lock()
        except Exception:
            pass
        self._token_lock_held = False

    def _save_state(self) -> None:
        if self._state is None:
            return
        try:
            self._state.save()
        except OSError as exc:
            logger.warning("[%s] Could not save read position to %s: %s", self.name, self._state.path, exc)

    # -- rooms ------------------------------------------------------------

    def _watched_rooms(self) -> List[str]:
        return [r for r in self._rooms if r in self._room_meta]

    async def _refresh_rooms(self, *, log_setup: bool = False) -> List[Dict[str, Any]]:
        rooms = await self._client.rooms()
        self._room_meta = {str(r.get("room_id")): r for r in rooms if r.get("room_id") is not None}
        if log_setup:
            self._log_room_setup()
        for rid in self._rooms:
            if rid not in self._room_meta and rid not in self._warned_rooms:
                self._warned_rooms.add(rid)
                logger.warning(
                    "[%s] Room %s is in CHATWORK_ROOMS but the account %s is not a member of it. "
                    "Invite the account to that room in Chatwork, or remove the id from CHATWORK_ROOMS.",
                    self.name, rid, self._bot_name or self._bot_id)
            elif rid in self._room_meta:
                self._warned_rooms.discard(rid)
        return rooms

    def _log_room_setup(self) -> None:
        if self._rooms:
            return
        listing = ", ".join(f"{rid} ({meta.get('name', '')})" for rid, meta in list(self._room_meta.items())[:20])
        logger.warning(
            "[%s] CHATWORK_ROOMS is empty, so Hermes will not answer anywhere yet. Rooms this account is in: %s. "
            "Add the ones Hermes should work in, e.g. CHATWORK_ROOMS=%s, to ~/.hermes/.env and restart. "
            "(The id is also the number after 'rid' in the room's URL.)",
            self.name, listing or "none — invite the account to a room first",
            next(iter(self._room_meta), "123456789"))

    # -- polling ----------------------------------------------------------

    async def _poll_loop(self) -> None:
        failures = 0
        while self._running:
            try:
                await self._poll_once()
                failures = 0
                delay = self._poll_interval
            except asyncio.CancelledError:
                raise
            except ChatworkAuthError as exc:
                logger.error("[%s] %s", self.name, exc)
                self._set_fatal_error("chatwork_unauthorized", str(exc), retryable=False)
                await self._notify_fatal_error()
                return
            except ChatworkRateLimited as exc:
                logger.warning("[%s] %s", self.name, exc)
                delay = max(self._poll_interval, exc.wait)
            except Exception as exc:  # network, 5xx, anything unexpected: back off, never die
                failures += 1
                delay = min(MAX_BACKOFF, self._poll_interval * (2 ** min(failures, 6)))
                delay *= 0.8 + random.random() * 0.4
                if isinstance(exc, ChatworkTransientError):
                    logger.warning("[%s] %s Next try in %.0fs.", self.name, exc, delay)
                else:
                    logger.exception("[%s] Unexpected error while checking Chatwork; next try in %.0fs.",
                                     self.name, delay)
            await self._sleep(delay)

    async def _poll_once(self) -> None:
        self._polls += 1
        await self._refresh_rooms()
        full = self._polls % FULL_REFRESH_EVERY == 1
        for rid in self._watched_rooms():
            meta = self._room_meta[rid]
            marker = (meta.get("last_update_time"), meta.get("message_num"))
            if not full and self._room_seen.get(rid) == marker and self._state.has_cursor(rid):
                continue
            try:
                await self._poll_room(rid)
            except ChatworkAccessError as exc:
                if rid not in self._warned_rooms:
                    self._warned_rooms.add(rid)
                    logger.warning("[%s] Cannot read room %s (%s). Check that the account is still a member.",
                                   self.name, rid, exc)
                continue
            self._room_seen[rid] = marker

    async def _poll_room(self, room_id: str) -> None:
        messages = await self._client.messages(room_id)  # outside the lock: replies never wait for polling
        # The lock only covers sorting out what is new. send() holds it from
        # POST to recording the new id, so in My Chat a reply that is already
        # visible here is always known to be ours by the time we look.
        async with self._io_lock:
            messages = sorted(messages, key=lambda m: int(m.get("message_id") or 0))
            if not self._state.has_cursor(room_id):
                newest = messages[-1]["message_id"] if messages else "0"
                self._state.cursors[room_id] = str(newest)
                self._save_state()
                logger.info("[%s] Watching room %s (%s) from now on; earlier messages are not answered.",
                            self.name, room_id, self._room_meta.get(room_id, {}).get("name", ""))
                return
            fresh = [m for m in messages if self._state.is_new(room_id, str(m.get("message_id")))]
            if not fresh:
                return
            if messages and len(messages) >= 100 and fresh[0] is messages[0]:
                logger.warning("[%s] More than 100 new messages in room %s since the last check; older ones "
                               "were skipped. Lower CHATWORK_POLL_INTERVAL if this room is that busy.",
                               self.name, room_id)
            by_id = {str(m.get("message_id")): m for m in messages}
            chosen = [(msg, parsed) for msg in fresh if (parsed := self._select(room_id, msg)) is not None]
            # Save the read position BEFORE answering: a crash now can lose a
            # reply, but a restart can never answer the same message twice.
            self._state.advance(room_id, str(fresh[-1].get("message_id")))
            self._save_state()
        # Outside the lock: fetching reply context may wait on the API.
        for msg, parsed in chosen:
            event = await self._event_for(room_id, msg, parsed, by_id)
            if await self._refuse_for_guest(event):
                continue
            await self.handle_message(event)

    def _guest_control(self, event: MessageEvent) -> Optional[str]:
        """What Hermes would treat this message as, if that is reserved for the token's account; else None.

        - A Hermes command other than OPEN_COMMANDS (aliases resolved the way
          Hermes resolves them). Command-looking text Hermes does not know (a
          skill name, a quick command, another plugin's command) is passed on
          as ordinary text: ``allow_gateway_control`` is turned off for it, so
          it reaches the model as words and runs nothing.
        - A bare approval word ("yes", "はい", "👍") while an approval is waiting
          in the sender's session, while their session is mid-turn
          (``_active_sessions``), or when session-key / approval / confirm helpers
          cannot be evaluated (fail closed). Also "always" while their /new or
          /reset confirmation is waiting — or fail closed if that helper fails.
        """
        try:  # "restart gateway" typed in a 1:1 chat becomes /restart inside Hermes; judge it the same way
            from gateway.platforms.base import coerce_plaintext_gateway_command
            coerce_plaintext_gateway_command(event)
        except Exception:
            pass
        cmd = event.get_command()
        if cmd:
            try:
                from hermes_cli.commands import resolve_command
                found = resolve_command(cmd)
            except Exception:  # cannot tell what it is: keep it away from the gateway
                found = None
            if found is None:
                event.allow_gateway_control = False
                return None
            return None if found.name in OPEN_COMMANDS else f"/{found.name}"
        word = (event.text or "").strip().lower()
        if not word or len(word) > 40:
            return None
        approval_words = _input_words(_APPROVAL_WORDS_0214, ("approve", "deny", "always", "session"))
        confirm_words = _input_words(_CONFIRM_ALWAYS_WORDS_0214, ("confirm_always",))
        confirm_reply = word.lstrip("!/")
        looks_reserved = word in approval_words or confirm_reply in confirm_words
        if not looks_reserved:
            return None
        # Fail closed: if session key / approval / confirm helpers cannot be evaluated,
        # treat reserved words as reserved (same posture as resolve_command above).
        try:
            key = self._event_session_key(event)
        except Exception:
            return word
        try:
            from tools.approval import has_blocking_approval
            approval_waiting = bool(has_blocking_approval(key))
        except Exception:
            return word
        try:
            from tools.slash_confirm import get_pending
            confirm_waiting = bool(get_pending(key))
        except Exception:
            return word
        # Also refuse approval words while this guest's session is mid-turn (race before
        # has_blocking_approval becomes true). If the attribute is missing (Hermes renamed
        # it), assume busy — fail closed rather than skipping the race guard.
        sessions = getattr(self, "_active_sessions", None)
        session_busy = True if sessions is None else key in sessions
        if (approval_waiting or session_busy) and word in approval_words:
            return word
        # Hermes reads a confirmation reply with "!" and "/" stripped ("!always" counts);
        # an approval reply it reads as typed (strip + lower), as compared above.
        if confirm_waiting and confirm_reply in confirm_words:
            return word
        return None

    async def _refuse_for_guest(self, event: MessageEvent) -> bool:
        """While CHATWORK_ALLOWED_USERS is unset, keep Hermes commands and approval answers to the token's account.

        Conversation is not affected: everyone in a listed room may still ask
        questions. The sender of a refused command gets the reason and the
        setting that changes it.
        """
        if event.source.user_id == self._bot_id or _allowed_users_set():
            return False
        refused = self._guest_control(event)
        if refused is None:
            return False
        logger.info("[%s] Refused %r from account %s in room %s: only the token's account may use Hermes commands "
                    "and answer approvals while CHATWORK_ALLOWED_USERS is unset.", self.name, refused,
                    event.source.user_id, event.source.chat_id)
        await self.send(event.source.chat_id, COMMAND_REFUSAL, reply_to=event.message_id)
        return True

    def _select(self, room_id: str, msg: Dict[str, Any]):
        """The parsed message if Hermes should answer it, else None. Runs under the lock."""
        mid = str(msg.get("message_id") or "")
        account = msg.get("account") or {}
        sender = str(account.get("account_id") or "")
        room_type = str(self._room_meta.get(room_id, {}).get("type") or "group")
        if self._state.was_sent_by_us(mid):
            return None
        # The bot's own messages are never input — except in My Chat, where you
        # and the bot are the same account (personal mode).
        if sender == self._bot_id and room_type != "my":
            return None
        parsed = parse_inbound(msg.get("body") or "", self._bot_id, (self._bot_name,))
        if parsed.is_system:
            return None
        replied_to_bot = parsed.reply_to_account == self._bot_id and (
            room_type != "my" or self._state.was_sent_by_us(parsed.reply_to_message or ""))
        addressed = parsed.to_bot or replied_to_bot
        if not addressed:
            if room_type == "my":
                if room_id not in self._free_rooms:
                    return None
            elif room_type == "group" and self._require_mention and room_id not in self._free_rooms:
                return None
        if not parsed.text.strip():
            logger.debug("[%s] Message %s addressed the bot but had no text; ignored.", self.name, mid)
            return None
        try:
            age = time.time() - int(msg.get("send_time") or 0)
        except (TypeError, ValueError):
            age = 0
        if age > MAX_CATCHUP_SECONDS:
            logger.info("[%s] Skipped message %s in room %s: it is %.0f hours old (sent while Hermes was not "
                        "running). Ask again if it still needs an answer.", self.name, mid, room_id, age / 3600)
            return None
        return parsed

    async def _event_for(self, room_id: str, msg: Dict[str, Any], parsed, by_id: Dict[str, Any]) -> MessageEvent:
        mid = str(msg.get("message_id") or "")
        account = msg.get("account") or {}
        sender = str(account.get("account_id") or "")
        room_type = str(self._room_meta.get(room_id, {}).get("type") or "group")
        replied_to_bot = parsed.reply_to_account == self._bot_id and (
            room_type != "my" or self._state.was_sent_by_us(parsed.reply_to_message or ""))
        reply_text = reply_author_id = reply_author_name = None
        if parsed.reply_to_message:
            original = by_id.get(parsed.reply_to_message)
            if original is None:
                try:  # context is optional: never hold up the answer for it
                    original = await self._client.message(room_id, parsed.reply_to_message,
                                                          max_wait=REPLY_CONTEXT_MAX_WAIT)
                except ChatworkError:
                    original = None
            if original:
                reply_text = quote_preview(original.get("body") or "")
                reply_author_id = str((original.get("account") or {}).get("account_id") or "") or None
                reply_author_name = (original.get("account") or {}).get("name")

        meta = self._room_meta.get(room_id, {})
        logger.info("[%s] Question from %s (account %s) in room %s (%s), message %s.", self.name,
                    account.get("name") or "?", sender, room_id, meta.get("name") or "", mid)
        source = self.build_source(
            chat_id=room_id, chat_name=meta.get("name") or room_id, chat_type=_chat_type(room_type),
            user_id=sender, user_name=account.get("name") or sender, message_id=mid)
        self._remember_inbound(mid, room_id, sender, account.get("name") or "")
        try:
            ts = datetime.fromtimestamp(int(msg.get("send_time") or 0), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            ts = datetime.now(tz=timezone.utc)
        return MessageEvent(
            text=parsed.text, message_type=MessageType.TEXT, source=source, message_id=mid,
            raw_message=msg, timestamp=ts,
            reply_to_message_id=parsed.reply_to_message, reply_to_text=reply_text,
            reply_to_author_id=reply_author_id, reply_to_author_name=reply_author_name,
            reply_to_is_own_message=bool(replied_to_bot))

    def _remember_inbound(self, mid: str, room_id: str, sender: str, name: str) -> None:
        self._inbound[mid] = (room_id, sender, name)
        while len(self._inbound) > 500:
            old, _ = self._inbound.popitem(last=False)
            self._addressed.discard(old)

    # -- outbound ---------------------------------------------------------

    def _bodies(self, content: str, reply_to: Optional[str], chat_id: str,
                metadata: Optional[Dict[str, Any]]) -> Tuple[List[str], str]:
        """Rendered message bodies, and the reply header put on the first one ("" if none)."""
        chunks = self.truncate_message(content or "", MAX_MESSAGE_LENGTH - 200)
        bodies = [render_outbound(c) for c in chunks]
        bodies = [b for b in bodies if b.strip()] or ["(empty reply)"]
        header = ""
        interim = bool((metadata or {}).get("_interim_send"))
        if reply_to and not interim and reply_to in self._inbound and reply_to not in self._addressed:
            room, aid, name = self._inbound[reply_to]
            if room == str(chat_id):
                header = reply_header(aid, room, reply_to, name)
                bodies[0] = header + bodies[0]
                self._addressed.add(reply_to)
        return bodies, header

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="Chatwork adapter is not connected", retryable=True)
        bodies, header = self._bodies(content, reply_to, chat_id, metadata)
        ids: List[str] = []
        try:
            for i, body in enumerate(bodies):
                async with self._io_lock:
                    mid = await self._client.post_message(chat_id, body)
                    if mid:
                        ids.append(mid)
                        if self._state is not None:
                            self._state.remember_sent(mid)
                            self._save_state()
                if mid and i == 0 and header:
                    self._headers[mid] = header
                    while len(self._headers) > 500:
                        self._headers.popitem(last=False)
        except ChatworkAccessError as exc:
            hint = (f"The account cannot post in room {chat_id}. In Chatwork, make sure it is a member with "
                    "permission to write (not read-only).") if exc.status == 403 else \
                f"Room {chat_id} was not found. Check the room id."
            logger.error("[%s] %s (%s)", self.name, hint, exc)
            return SendResult(success=False, error=hint, error_kind="forbidden" if exc.status == 403 else "not_found")
        except ChatworkRateLimited as exc:
            return SendResult(success=False, error=str(exc), retryable=True, retry_after=exc.wait,
                              error_kind="rate_limited")
        except ChatworkAuthError as exc:
            return SendResult(success=False, error=str(exc), error_kind="forbidden")
        except ChatworkError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable,
                              error_kind="transient" if exc.retryable else "unknown")
        if not ids:
            return SendResult(success=False, error="Chatwork did not return a message id", retryable=True)
        return SendResult(success=True, message_id=ids[-1], continuation_message_ids=tuple(ids[:-1]))

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        """Chatwork can edit a message (tool-progress bubbles use this)."""
        if self._client is None:
            return SendResult(success=False, error="Chatwork adapter is not connected", retryable=True)
        header = self._headers.get(str(message_id), "")
        body = header + (render_outbound((content or "")[: MAX_MESSAGE_LENGTH - 200]) or "…")
        try:
            mid = await self._client.update_message(chat_id, message_id, body)
        except ChatworkRateLimited as exc:
            return SendResult(success=False, error=str(exc), retryable=True, retry_after=exc.wait,
                              error_kind="rate_limited")
        except ChatworkAccessError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found" if exc.status == 404 else "forbidden")
        except ChatworkError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        return SendResult(success=True, message_id=mid)

    async def send_private_notice(self, chat_id: str, user_id: Optional[str], content: str,
                                  reply_to: Optional[str] = None,
                                  metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Hermes' setup and operational notices for one person (``notice_delivery: private``).

        Chatwork has no message that only one member of a room can see, so in a
        group room the notice is written to the gateway log instead of the room
        (this keeps, for example, the "No home channel is set ... /sethome" hint
        away from everyone in a team room). In My Chat and 1:1 chats it is sent
        as usual.
        """
        if _chat_type(str(self._room_meta.get(str(chat_id), {}).get("type") or "group")) == "group":
            logger.warning("[%s] Notice for account %s kept out of group room %s (notice_delivery=private): %s",
                           self.name, user_id or "?", chat_id, content)
            return SendResult(success=True)
        return await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None  # Chatwork has no typing indicator

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        meta = self._room_meta.get(str(chat_id), {})
        return {"name": meta.get("name") or str(chat_id), "type": _chat_type(str(meta.get("type") or "group"))}


# --- module-level hooks for the platform registry ---------------------------

def check_requirements() -> bool:
    """Passive probe: is a token configured? (httpx ships with Hermes.)"""
    return bool(str(_get_scoped_secret("CHATWORK_API_TOKEN", "") or "").strip())


def validate_config(config) -> bool:
    return bool(_token_from(getattr(config, "extra", None)))


def is_connected(config) -> bool:
    return validate_config(config) or check_requirements()


def _env_enablement() -> Optional[dict]:
    token = str(_get_scoped_secret("CHATWORK_API_TOKEN", "") or "").strip()
    if not token:
        return None
    seed: Dict[str, Any] = {"token": token}
    for env, key in (("CHATWORK_ROOMS", "rooms"), ("CHATWORK_FREE_RESPONSE_ROOMS", "free_response_rooms"),
                     ("CHATWORK_REQUIRE_MENTION", "require_mention"), ("CHATWORK_POLL_INTERVAL", "poll_interval")):
        val = str(_get_scoped_secret(env, "") or "").strip()
        if val:
            seed[key] = val
    # Hermes' per-person notices (such as the one-time "No home channel is set
    # ... /sethome" hint) stay out of group rooms unless the user asks for them
    # there; see ChatworkAdapter.send_private_notice.
    delivery = str(_get_scoped_secret("CHATWORK_NOTICE_DELIVERY", "") or "").strip().lower()
    seed["notice_delivery"] = delivery if delivery in ("public", "private") else "private"
    # Home room for cron / `hermes send` and Hermes' own notices. The plugin
    # never picks one itself: unset means Hermes has no Chatwork home.
    home = _csv_ids(_get_scoped_secret("CHATWORK_HOME_CHANNEL", ""))
    if home:
        seed["home_channel"] = {"chat_id": home[0],
                                "name": str(_get_scoped_secret("CHATWORK_HOME_CHANNEL_NAME", "") or "Chatwork")}
    return seed


_YAML_BRIDGE = (
    ("rooms", "CHATWORK_ROOMS", "csv"),
    ("free_response_rooms", "CHATWORK_FREE_RESPONSE_ROOMS", "csv"),
    ("require_mention", "CHATWORK_REQUIRE_MENTION", "lower"),
    ("poll_interval", "CHATWORK_POLL_INTERVAL", "str"),
)


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    try:
        from gateway.platforms._shared import apply_yaml_bridge
    except ImportError:  # pragma: no cover
        return None
    return apply_yaml_bridge(platform_cfg or {}, _YAML_BRIDGE)


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[List[str]] = None, force_document: bool = False) -> Dict[str, Any]:
    """Out-of-process delivery for cron / `hermes send` when no gateway is running."""
    extra = getattr(pconfig, "extra", None) or {}
    token = _token_from(extra)
    home = extra.get("home_channel") or {}
    room = _csv_ids(chat_id or (home.get("chat_id") if isinstance(home, dict) else "")
                    or _get_scoped_secret("CHATWORK_HOME_CHANNEL", ""))
    if not token:
        return {"error": "Chatwork: CHATWORK_API_TOKEN is not set."}
    if not room:
        return {"error": "Chatwork: no room to deliver to. Set CHATWORK_HOME_CHANNEL to a room id."}
    client = ChatworkClient(token)
    ids: List[str] = []
    try:
        for chunk in BasePlatformAdapter.truncate_message(message or "", MAX_MESSAGE_LENGTH - 200):
            body = render_outbound(chunk)
            if body.strip():
                ids.append(await client.post_message(room[0], body))
    except ChatworkError as exc:
        return {"error": f"Chatwork: {exc}"}
    finally:
        await client.aclose()
    return {"success": True, "platform": PLATFORM_NAME, "chat_id": room[0], "message_id": ids[-1] if ids else ""}


def _parse_target(raw: str):
    """`hermes send --to chatwork:123456` (or `rid123456`, as in the room URL)."""
    s = str(raw or "").strip()
    if s.lower().startswith("rid"):
        s = s[3:]
    return (s, None) if s.isdigit() else None


def register(ctx) -> None:
    """Plugin entry point."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Chatwork",
        adapter_factory=lambda cfg: ChatworkAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CHATWORK_API_TOKEN", "CHATWORK_ROOMS"],
        install_hint="Set CHATWORK_API_TOKEN (and CHATWORK_ROOMS) in ~/.hermes/.env — no extra packages needed.",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="CHATWORK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        parse_target_ref_fn=_parse_target,
        allowed_users_env="CHATWORK_ALLOWED_USERS",
        allow_all_env="CHATWORK_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        pii_safe=False,
        emoji="💬",
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
    )
