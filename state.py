"""Durable read position, so a restart never answers the same message twice.

Stored as JSON in ``<HERMES_HOME>/plugin-data/jp-chatwork/state.json``:

* ``rooms[<room_id>].cursor`` — the newest message id already handled.
  The adapter saves it *before* handing a message to the agent, so a crash in
  the middle loses at most one reply instead of sending it twice.
* ``sent`` — ids of messages this adapter posted (bounded). In My Chat the
  bot and the human are the same account, so this is how the adapter tells
  its own replies apart from your input.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_VERSION = 1
MAX_SENT_IDS = 1000


def _as_int(message_id: Optional[str]) -> int:
    try:
        return int(message_id or 0)
    except (TypeError, ValueError):
        return 0


class CursorState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.account_id: str = ""
        self.cursors: Dict[str, str] = {}
        self._sent: List[str] = []
        self._sent_set: set = set()

    # -- persistence -------------------------------------------------------

    def load(self) -> "CursorState":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self
        except (OSError, ValueError) as exc:
            # A broken file must not stop the bot; start fresh but say so.
            logger.warning("Chatwork: could not read %s (%s); starting from the newest messages.", self.path, exc)
            return self
        if not isinstance(raw, dict):
            return self
        self.account_id = str(raw.get("account_id") or "")
        rooms = raw.get("rooms") or {}
        if isinstance(rooms, dict):
            for room_id, info in rooms.items():
                if isinstance(info, dict) and info.get("cursor"):
                    self.cursors[str(room_id)] = str(info["cursor"])
        sent = raw.get("sent") or []
        if isinstance(sent, list):
            self._sent = [str(s) for s in sent][-MAX_SENT_IDS:]
            self._sent_set = set(self._sent)
        return self

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "account_id": self.account_id,
            "rooms": {rid: {"cursor": cur} for rid, cur in sorted(self.cursors.items())},
            "sent": self._sent[-MAX_SENT_IDS:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- cursor ------------------------------------------------------------

    def has_cursor(self, room_id: str) -> bool:
        return str(room_id) in self.cursors

    def is_new(self, room_id: str, message_id: str) -> bool:
        return _as_int(message_id) > _as_int(self.cursors.get(str(room_id)))

    def advance(self, room_id: str, message_id: str) -> None:
        if self.is_new(room_id, message_id):
            self.cursors[str(room_id)] = str(message_id)

    # -- own messages ------------------------------------------------------

    def remember_sent(self, message_id: str) -> None:
        mid = str(message_id or "")
        if not mid or mid in self._sent_set:
            return
        self._sent.append(mid)
        self._sent_set.add(mid)
        if len(self._sent) > MAX_SENT_IDS:
            drop = self._sent[: len(self._sent) - MAX_SENT_IDS]
            self._sent = self._sent[-MAX_SENT_IDS:]
            self._sent_set.difference_update(drop)

    def was_sent_by_us(self, message_id: str) -> bool:
        return str(message_id or "") in self._sent_set
