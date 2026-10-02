"""Adapter behaviour against an in-memory Chatwork."""

import asyncio
import time
from pathlib import Path

from gateway.config import PlatformConfig

import adapter as cw
from client import ChatworkAuthError, ChatworkTransientError

BOT = 900
HUMAN = 111
TEAM = "1001"  # group room the bot is in and is allowed to use
OTHER = "1002"  # group room the bot is in but NOT allowed to use
MYCHAT = "1003"  # the account's own My Chat
DIRECT = "1004"  # 1:1 chat with the bot


class FakeChatwork:
    """Just enough of the Chatwork API, with ids that increase like the real ones."""

    def __init__(self, me_id=BOT, me_name="AIアシスタント"):
        self.me_id, self.me_name = me_id, me_name
        self.rooms_meta = {
            TEAM: {"room_id": int(TEAM), "type": "group", "name": "営業チーム"},
            OTHER: {"room_id": int(OTHER), "type": "group", "name": "雑談"},
            MYCHAT: {"room_id": int(MYCHAT), "type": "my", "name": "マイチャット"},
            DIRECT: {"room_id": int(DIRECT), "type": "direct", "name": "山田"},
        }
        self.msgs = {rid: [] for rid in self.rooms_meta}
        self.next_id = 10_000_000_000
        self.posted = []
        self.edited = []
        self.fail_next = []  # exceptions raised by the next calls, in order
        self.calls = 0

    def add(self, room, body, aid=HUMAN, name="山田", age=0):
        self.next_id += 7
        mid = str(self.next_id)
        self.msgs[room].append({"message_id": mid, "account": {"account_id": aid, "name": name},
                                "body": body, "send_time": int(time.time() - age), "update_time": 0})
        meta = self.rooms_meta[room]
        meta["message_num"] = len(self.msgs[room])
        meta["last_update_time"] = self.next_id
        return mid

    def _maybe_fail(self):
        self.calls += 1
        if self.fail_next:
            raise self.fail_next.pop(0)

    async def me(self):
        self._maybe_fail()
        return {"account_id": self.me_id, "name": self.me_name, "room_id": int(MYCHAT)}

    async def rooms(self):
        self._maybe_fail()
        return [dict(m) for m in self.rooms_meta.values()]

    async def messages(self, room_id):
        self._maybe_fail()
        return list(self.msgs[str(room_id)][-100:])

    async def message(self, room_id, message_id, max_wait=None):
        self._maybe_fail()
        self.message_max_wait = max_wait
        return next(m for m in self.msgs[str(room_id)] if m["message_id"] == str(message_id))

    async def post_message(self, room_id, body):
        self._maybe_fail()
        mid = self.add(str(room_id), body, aid=self.me_id, name=self.me_name)
        self.posted.append((str(room_id), body, mid))
        return mid

    async def update_message(self, room_id, message_id, body):
        self._maybe_fail()
        self.edited.append((str(room_id), str(message_id), body))
        return str(message_id)

    async def aclose(self):
        pass


def make_adapter(fake, tmp_path: Path, **extra):
    cfg = PlatformConfig(enabled=True, extra={"token": "x" * 32, "rooms": f"{TEAM},{MYCHAT},{DIRECT}", **extra})
    a = cw.ChatworkAdapter(cfg, client=fake, state_path=tmp_path / "state.json")
    a.received = []

    async def capture(event):
        a.received.append(event)

    a.handle_message = capture
    return a


async def start(a):
    assert await a.connect()
    a._poll_task.cancel()  # tests drive polling by hand
    try:
        await a._poll_task
    except asyncio.CancelledError:
        pass
    await a._poll_once()  # first look at each room: sets the read position


def run(coro):
    return asyncio.run(coro)


def test_answers_only_when_addressed_in_group(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        fake.add(TEAM, "お疲れさまです")  # not for the bot
        to_mid = fake.add(TEAM, "[To:900]AIアシスタントさん\n見積の書き方を教えて")
        await a._poll_once()
        return to_mid

    to_mid = run(scenario())
    assert [e.message_id for e in a.received] == [to_mid]
    ev = a.received[0]
    assert ev.text == "見積の書き方を教えて"
    assert ev.source.chat_id == TEAM and ev.source.chat_type == "group"
    assert ev.source.user_id == str(HUMAN) and ev.source.user_name == "山田"


def test_reply_to_bot_is_answered_with_context(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        bot_mid = fake.add(TEAM, "前回の回答です", aid=BOT, name="AIアシスタント")
        fake.add(TEAM, f"[rp aid=900 to={TEAM}-{bot_mid}]AIアシスタントさん\nもう少し詳しく")
        await a._poll_once()
        return bot_mid

    bot_mid = run(scenario())
    assert len(a.received) == 1
    ev = a.received[0]
    assert ev.text == "もう少し詳しく"
    assert ev.reply_to_message_id == bot_mid and ev.reply_to_is_own_message
    assert ev.reply_to_text == "前回の回答です"


def test_room_not_in_allowlist_is_ignored(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        fake.add(OTHER, "[To:900]AIアシスタントさん\n社外秘を教えて")
        await a._poll_once()

    run(scenario())
    assert a.received == []


def test_history_before_start_is_not_answered(tmp_path):
    fake = FakeChatwork()
    fake.add(TEAM, "[To:900]AIアシスタントさん\n昨日の質問")
    a = make_adapter(fake, tmp_path)
    run(start(a))
    assert a.received == []


def test_no_double_reply_across_restart(tmp_path):
    fake = FakeChatwork()
    first = make_adapter(fake, tmp_path)

    async def phase1():
        await start(first)
        fake.add(TEAM, "[To:900]AIアシスタントさん\n質問1")
        await first._poll_once()
        await first.disconnect()

    run(phase1())
    assert len(first.received) == 1

    second = make_adapter(fake, tmp_path)  # same state file = a restart

    async def phase2():
        await start(second)
        await second._poll_once()
        fake.add(TEAM, "[To:900]AIアシスタントさん\n質問2")
        await second._poll_once()

    run(phase2())
    assert [e.text for e in second.received] == ["質問2"]


def test_questions_from_downtime_are_answered_but_not_stale_ones(tmp_path):
    fake = FakeChatwork()
    first = make_adapter(fake, tmp_path)

    async def phase1():
        await start(first)
        await first.disconnect()

    run(phase1())
    fake.add(TEAM, "[To:900]AIアシスタントさん\n先週の質問", age=8 * 24 * 3600)
    fake.add(TEAM, "[To:900]AIアシスタントさん\n停止中の質問", age=600)
    second = make_adapter(fake, tmp_path)
    run(start(second))
    assert [e.text for e in second.received] == ["停止中の質問"]


def test_own_messages_are_ignored_in_group(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        fake.add(TEAM, "[To:900]自分宛てのメモ", aid=BOT)
        await a._poll_once()

    run(scenario())
    assert a.received == []


def test_direct_chat_needs_no_to(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        fake.add(DIRECT, "議事録を要約して")
        await a._poll_once()

    run(scenario())
    assert [e.text for e in a.received] == ["議事録を要約して"]
    assert a.received[0].source.chat_type == "dm"


def test_require_mention_false_answers_everything_in_group(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path, require_mention="false")

    async def scenario():
        await start(a)
        fake.add(TEAM, "今日の天気は？")
        await a._poll_once()

    run(scenario())
    assert len(a.received) == 1


def test_free_response_room(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path, free_response_rooms=TEAM)

    async def scenario():
        await start(a)
        fake.add(TEAM, "誰か分かる？")
        await a._poll_once()

    run(scenario())
    assert len(a.received) == 1


def test_my_chat_personal_mode_and_no_self_loop(tmp_path):
    """One account plays both roles in My Chat: its [To:self] is input, its replies are not."""
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        fake.add(MYCHAT, "買い物メモ: 牛乳", aid=BOT)  # a plain note: ignored
        q = fake.add(MYCHAT, "[To:900] 2+3は？", aid=BOT)
        await a._poll_once()
        assert [e.message_id for e in a.received] == [q]
        res = await a.send(MYCHAT, "5です。", reply_to=q)
        assert res.success
        await a._poll_once()  # the reply (also by account 900, with [rp aid=900]) must not loop
        follow = fake.add(MYCHAT, f"[rp aid=900 to={MYCHAT}-{res.message_id}]AIアシスタントさん\nでは3+4は？", aid=BOT)
        await a._poll_once()
        return follow

    follow = run(scenario())
    assert [e.message_id for e in a.received][-1] == follow
    assert len(a.received) == 2


def test_send_addresses_asker_once_and_formats(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        q = fake.add(TEAM, "[To:900]AIアシスタントさん\nコマンドは？")
        await a._poll_once()
        r1 = await a.send(TEAM, "こちらです\n```\nls -la\n```\n[toall]", reply_to=q)
        r2 = await a.send(TEAM, "補足です", reply_to=q)
        return q, r1, r2

    q, r1, r2 = run(scenario())
    assert r1.success and r2.success
    body1, body2 = fake.posted[0][1], fake.posted[1][1]
    assert body1.startswith(f"[rp aid={HUMAN} to={TEAM}-{q}]山田さん\n")
    assert "[code]ls -la[/code]" in body1 and "[toall]" not in body1
    assert not body2.startswith("[rp")  # one notification per question


def test_long_answer_is_split(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        return await a.send(TEAM, ("長い行です。" * 50 + "\n") * 80)

    res = run(scenario())
    assert res.success and len(fake.posted) >= 2
    assert all(len(b) <= cw.MAX_MESSAGE_LENGTH for _, b, _ in fake.posted)
    assert res.continuation_message_ids


def test_interim_messages_do_not_notify(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        q = fake.add(TEAM, "[To:900]AIアシスタントさん\n調べて")
        await a._poll_once()
        await a.send(TEAM, "調べています…", reply_to=q, metadata={"_interim_send": True})
        await a.send(TEAM, "結果です", reply_to=q)

    run(scenario())
    assert not fake.posted[0][1].startswith("[rp")
    assert fake.posted[1][1].startswith("[rp")


def test_edit_message_uses_put(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        return await a.edit_message(TEAM, "123", "**途中経過**")

    res = run(scenario())
    assert res.success and fake.edited == [(TEAM, "123", "途中経過")]


def test_bad_token_stops_with_instructions(tmp_path):
    fake = FakeChatwork()
    fake.fail_next = [ChatworkAuthError("Chatwork rejected the API token (401). ... CHATWORK_API_TOKEN ...")]
    a = make_adapter(fake, tmp_path)
    assert run(a.connect()) is False
    assert a.has_fatal_error and a.fatal_error_retryable is False
    assert "CHATWORK_API_TOKEN" in a.fatal_error_message


def test_missing_token_stops_with_instructions(tmp_path):
    cfg = PlatformConfig(enabled=True, extra={"rooms": TEAM})
    a = cw.ChatworkAdapter(cfg, client=FakeChatwork(), state_path=tmp_path / "s.json")
    assert run(a.connect()) is False
    assert "token.php" in a.fatal_error_message


def test_poll_loop_survives_network_errors(tmp_path, monkeypatch):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path, poll_interval="2")
    slept = []

    async def fast_sleep(sec):
        slept.append(sec)
        if len(slept) >= 3:
            a._running = False

    async def scenario():
        await start(a)
        fake.fail_next = [ChatworkTransientError("net down"), ChatworkTransientError("net down")]
        a._sleep = fast_sleep
        a._running = True
        await a._poll_loop()

    run(scenario())
    assert not a.has_fatal_error
    assert slept[0] > 2 and slept[1] > slept[0] * 1.2  # backs off
    assert slept[2] == 2.0  # back to normal once Chatwork answers


def test_poll_loop_stops_on_revoked_token(tmp_path, monkeypatch):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        fake.fail_next = [ChatworkAuthError("revoked; set CHATWORK_API_TOKEN")]
        a._running = True
        await a._poll_loop()

    run(scenario())
    assert a.has_fatal_error and not a.fatal_error_retryable


def test_room_ids_accept_rid_prefix_and_spaces():
    assert cw._csv_ids(" rid123, 456 ;abc,123") == ["123", "456"]
    assert cw._parse_target("rid42") == ("42", None)
    assert cw._parse_target("#general") is None


def test_env_enablement_seeds_settings(monkeypatch):
    monkeypatch.setenv("CHATWORK_API_TOKEN", "x" * 32)
    monkeypatch.setenv("CHATWORK_ROOMS", f"rid{TEAM}, {DIRECT}")
    seed = cw._env_enablement()
    assert seed["token"] == "x" * 32 and seed["rooms"] == f"rid{TEAM}, {DIRECT}"
    assert "home_channel" not in seed  # never a team room by default
    monkeypatch.setenv("CHATWORK_HOME_CHANNEL", DIRECT)
    assert cw._env_enablement()["home_channel"]["chat_id"] == DIRECT
    monkeypatch.delenv("CHATWORK_API_TOKEN")
    assert cw._env_enablement() is None


def test_connect_never_sets_a_home_room(tmp_path):
    """The home room is the user's choice (CHATWORK_HOME_CHANNEL or /sethome); the plugin never writes it."""
    a = make_adapter(FakeChatwork(), tmp_path)
    run(start(a))
    assert a.config.home_channel is None

    from gateway.config import HomeChannel
    b = make_adapter(FakeChatwork(), tmp_path / "b")
    b.config.home_channel = HomeChannel(platform=b.platform, chat_id=TEAM, name="team")
    run(start(b))
    assert b.config.home_channel.chat_id == TEAM


# --- added after review -------------------------------------------------------

def test_streaming_edits_are_off_but_progress_edits_keep_the_header(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)
    assert cw.ChatworkAdapter.SUPPORTS_MESSAGE_EDITING is False

    async def scenario():
        await start(a)
        q = fake.add(TEAM, "[To:900]AIアシスタントさん\n進めて")
        await a._poll_once()
        r = await a.send(TEAM, "途中", reply_to=q)
        await a.edit_message(TEAM, r.message_id, "完了しました")
        return q

    q = run(scenario())
    assert fake.edited[-1][2] == f"[rp aid={HUMAN} to={TEAM}-{q}]山田さん\n完了しました"


def test_send_failures_are_classified(tmp_path):
    from client import ChatworkAccessError, ChatworkRateLimited
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        out = []
        for exc in (ChatworkAccessError("x", 403), ChatworkAccessError("x", 404), ChatworkRateLimited("x", 42.0),
                    ChatworkTransientError("x")):
            fake.fail_next = [exc]
            out.append(await a.send(TEAM, "hi"))
        return out

    forbidden, missing, limited, transient = run(scenario())
    assert not forbidden.success and forbidden.error_kind == "forbidden" and "read-only" in forbidden.error
    assert missing.error_kind == "not_found"
    assert limited.retryable and limited.retry_after == 42.0 and limited.error_kind == "rate_limited"
    assert transient.retryable


def test_unchanged_rooms_are_not_refetched_until_the_periodic_full_check(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)
    calls = []
    original = fake.messages

    async def counting(room_id):
        calls.append(str(room_id))
        return await original(room_id)

    fake.messages = counting

    async def scenario():
        await start(a)  # poll 1: full
        calls.clear()
        await a._poll_once()  # poll 2: nothing changed
        assert calls == []
        fake.add(TEAM, "雑談")
        await a._poll_once()  # poll 3: only the changed room
        assert calls == [TEAM]
        calls.clear()
        a._polls = cw.FULL_REFRESH_EVERY  # next poll is a full one
        await a._poll_once()
        assert sorted(calls) == sorted([TEAM, MYCHAT, DIRECT])

    run(scenario())


def test_more_than_100_new_messages_is_logged(tmp_path, caplog):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        for i in range(105):
            fake.add(TEAM, f"msg {i}")
        await a._poll_once()

    with caplog.at_level("WARNING"):
        run(scenario())
    assert any("More than 100 new messages" in r.getMessage() for r in caplog.records)


def test_token_already_in_use_stops_connect(tmp_path, monkeypatch):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)
    monkeypatch.setattr(a, "_acquire_platform_lock", lambda *args: False)
    assert run(a.connect()) is False
    assert fake.calls == 0  # nothing polled with a token another gateway owns


def test_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    fake = FakeChatwork()
    fake.add(TEAM, "[To:900]AIアシスタントさん\n古い質問")
    a = make_adapter(fake, tmp_path)
    run(start(a))
    assert a.received == []  # starts from the newest message, answers nothing old
    assert '"cursor"' in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_standalone_send_needs_a_room_and_never_picks_one(monkeypatch):
    fake = FakeChatwork()
    monkeypatch.setattr(cw, "ChatworkClient", lambda token: fake)
    cfg = PlatformConfig(enabled=True, extra={"token": "x" * 32})
    res = run(cw._standalone_send(cfg, "", "定時レポート"))
    assert "CHATWORK_HOME_CHANNEL" in res["error"] and fake.calls == 0 and fake.posted == []
    res = run(cw._standalone_send(cfg, TEAM, "定時レポート\n```\nok\n```"))
    assert res["success"] and res["chat_id"] == TEAM
    assert fake.posted[-1][1] == "定時レポート\n[code]ok[/code]"
    monkeypatch.setenv("CHATWORK_HOME_CHANNEL", DIRECT)
    assert run(cw._standalone_send(cfg, "", "ホームへ"))["chat_id"] == DIRECT
    assert "CHATWORK_API_TOKEN" in run(cw._standalone_send(PlatformConfig(enabled=True, extra={}), TEAM, "x"))["error"]


def _runner_with(adapter):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = None
    runner.config = GatewayConfig(platforms={adapter.platform: adapter.config})
    runner._primary_profile_name = "default"
    runner.adapters = {adapter.platform: adapter}
    runner._profile_adapters = {}
    return runner


def _src(adapter, user_id, chat_type="group"):
    from gateway.session import SessionSource
    return SessionSource(platform=adapter.platform, chat_id=TEAM, chat_type=chat_type, user_id=user_id,
                         user_name="x", is_bot=False)


def test_gateway_authorizes_everyone_in_listed_rooms_by_default(tmp_path, monkeypatch):
    for var in ("GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(var, raising=False)
    a = make_adapter(FakeChatwork(), tmp_path)
    runner = _runner_with(a)
    assert runner._is_user_authorized(_src(a, "222")) is True
    assert runner._is_user_authorized(_src(a, "333", "dm")) is True


def test_allowed_users_narrows_who_can_use_it(tmp_path, monkeypatch):
    for var in ("GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHATWORK_ALLOWED_USERS", str(HUMAN))
    a = make_adapter(FakeChatwork(), tmp_path)
    runner = _runner_with(a)
    assert runner._is_user_authorized(_src(a, str(HUMAN))) is True
    assert runner._is_user_authorized(_src(a, "222")) is False


def test_room_removed_then_added_back_starts_fresh(tmp_path):
    """A question asked while the room was not allowed is not answered later."""
    fake = FakeChatwork()
    run(start(make_adapter(fake, tmp_path)))
    fake.add(TEAM, "[To:900]AIアシスタントさん\n許可外の間の質問")
    without_team = make_adapter(fake, tmp_path, rooms=f"{MYCHAT},{DIRECT}")
    run(start(without_team))
    assert without_team.received == []
    back = make_adapter(fake, tmp_path)
    run(start(back))
    assert back.received == []


def test_reply_context_fetch_is_bounded_and_outside_history(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        old = fake.add(TEAM, "かなり前の発言", age=60)
        fake.msgs[TEAM] = [m for m in fake.msgs[TEAM] if m["message_id"] != old] + []
        fake.msgs[TEAM].insert(0, {"message_id": old, "account": {"account_id": HUMAN, "name": "山田"},
                                   "body": "かなり前の発言", "send_time": 0, "update_time": 0})
        for _ in range(100):
            fake.add(TEAM, "雑談")
        fake.add(TEAM, f"[To:900]AIアシスタントさん\n[rp aid={HUMAN} to={TEAM}-{old}]山田さん\nこれについて")
        await a._poll_once()

    run(scenario())
    assert a.received and a.received[-1].reply_to_text == "かなり前の発言"
    assert fake.message_max_wait == cw.REPLY_CONTEXT_MAX_WAIT


# --- approvals: only the token's account answers while CHATWORK_ALLOWED_USERS is unset ----------

def _ask(fake, room, text, aid=HUMAN):
    return fake.add(room, f"[To:900]AIアシスタントさん\n{text}" if room == TEAM else text, aid=aid)


def test_approval_commands_from_others_are_refused_with_the_reason(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        asks = [_ask(fake, TEAM, cmd) for cmd in ("/approve", "/deny 危ないので", "/yolo", "/approvals off")]
        asks.append(_ask(fake, DIRECT, "/approve always"))
        question = _ask(fake, TEAM, "見積の書き方を教えて")
        await a._poll_once()
        return asks, question

    asks, question = run(scenario())
    assert [e.message_id for e in a.received] == [question]  # the conversation itself goes on
    refusals = [(room, body) for room, body, _ in fake.posted]
    assert len(refusals) == len(asks)
    for (room, body), mid in zip(refusals, asks):
        assert body.startswith(f"[rp aid={HUMAN} to={room}-{mid}]山田さん\n")
        assert "CHATWORK_ALLOWED_USERS" in body and "/approve" in body


def test_owner_answers_approvals_in_my_chat(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        mid = fake.add(MYCHAT, "[To:900] /approve", aid=BOT)
        await a._poll_once()
        return mid

    mid = run(scenario())
    assert [(e.message_id, e.text) for e in a.received] == [(mid, "/approve")]
    assert fake.posted == []


def test_allowed_users_hand_approvals_back_to_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATWORK_ALLOWED_USERS", str(HUMAN))
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        _ask(fake, TEAM, "/approve")
        await a._poll_once()

    run(scenario())
    assert [e.text for e in a.received] == ["/approve"] and fake.posted == []


def test_bare_yes_is_refused_only_while_an_approval_waits(tmp_path, monkeypatch):
    """Hermes reads a bare "yes" as an answer only while an approval blocks that session."""
    from gateway.session import SessionSource, build_session_key
    import tools.approval as approval

    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)
    # The key the gateway files the asker's approval under (default config: one session per person in a group).
    key = build_session_key(SessionSource(platform=a.platform, chat_id=TEAM, chat_type="group",
                                          user_id=str(HUMAN), user_name="山田"))

    async def scenario():
        await start(a)
        idle_yes = _ask(fake, TEAM, "yes")
        await a._poll_once()
        monkeypatch.setitem(approval._gateway_queues, key, [object()])
        assert approval.has_blocking_approval(key)
        waiting = [_ask(fake, TEAM, w) for w in ("yes", " OK ", "👍")]
        other = _ask(fake, TEAM, "yes, but what does it delete?")
        someone_else = fake.add(TEAM, "[To:900]AIアシスタントさん\nyes", aid=222, name="佐藤")
        await a._poll_once()
        return idle_yes, waiting, other, someone_else

    idle_yes, waiting, other, someone_else = run(scenario())
    # Nothing waiting: "yes" is ordinary conversation. Waiting: refused. Longer text and a
    # person whose own session has nothing waiting go through as usual.
    assert [e.message_id for e in a.received] == [idle_yes, other, someone_else]
    assert [mid for _, body, _ in fake.posted for mid in waiting if f"-{mid}]" in body] == waiting


def test_guests_keep_only_help_whoami_new_and_reset(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)

    async def scenario():
        await start(a)
        refused = [_ask(fake, TEAM, c) for c in ("/sethome", "/pause", "/model x --global", "/memory approve 1")]
        refused.append(_ask(fake, DIRECT, "restart gateway"))  # Hermes turns this into /restart in a 1:1 chat
        passed = [_ask(fake, TEAM, c) for c in ("/help", "/new", "/whoami", "/reset")]
        skill = _ask(fake, TEAM, "/meeting-notes 議事録にして")  # not a Hermes command: plain text
        await a._poll_once()
        return refused, passed, skill

    refused, passed, skill = run(scenario())
    assert [e.message_id for e in a.received] == passed + [skill]
    assert all(e.allow_gateway_control for e in a.received[:-1])
    assert a.received[-1].allow_gateway_control is False and a.received[-1].get_command() is None
    replied_to = [mid for _, body, _ in fake.posted for mid in refused if f"-{mid}]" in body]
    assert replied_to == refused
    assert all("/sethome" in body and "/help" in body for _, body, _ in fake.posted)


def test_owner_keeps_every_command(tmp_path):
    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path, free_response_rooms=MYCHAT)

    async def scenario():
        await start(a)
        for c in ("/sethome", "/model x --global"):
            fake.add(MYCHAT, c, aid=BOT)
        await a._poll_once()

    run(scenario())
    assert [e.text for e in a.received] == ["/sethome", "/model x --global"] and fake.posted == []


def test_private_notices_stay_out_of_group_rooms(tmp_path, monkeypatch):
    """The one-time "No home channel is set ... /sethome" hint is not shown to a whole team room."""
    monkeypatch.setenv("CHATWORK_API_TOKEN", "x" * 32)
    assert cw._env_enablement()["notice_delivery"] == "private"
    monkeypatch.setenv("CHATWORK_NOTICE_DELIVERY", "public")
    assert cw._env_enablement()["notice_delivery"] == "public"

    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path, notice_delivery="private")
    runner = _runner_with(a)

    async def scenario():
        await start(a)
        await runner._deliver_platform_notice(_src(a, str(HUMAN)), "📬 No home channel is set for Chatwork. /sethome")
        assert fake.posted == []
        direct = _src(a, str(HUMAN), "dm")
        direct.chat_id = DIRECT
        await runner._deliver_platform_notice(direct, "📬 No home channel is set for Chatwork. /sethome")

    run(scenario())
    assert [room for room, _, _ in fake.posted] == [DIRECT]


def test_guest_cannot_answer_always_to_a_pending_reset_confirmation(tmp_path, monkeypatch):
    """Hermes strips "!" and "/" from a confirmation reply, so "!always" and "!常に" answer it too."""
    from gateway.session import SessionSource, build_session_key
    import tools.slash_confirm as slash_confirm

    monkeypatch.setenv("HERMES_LANGUAGE", "ja")
    from gateway.run_inbound import GatewayInboundMixin
    inbound = object.__new__(GatewayInboundMixin)
    # The words Hermes itself maps to "always": newer builds add the active language's, 0.21.4 has a fixed table.
    choices = (inbound._slash_confirm_text_choices() if hasattr(inbound, "_slash_confirm_text_choices")
               else GatewayInboundMixin._SLASH_CONFIRM_TEXT_CHOICES)
    always_words = sorted(w for w, choice in choices.items() if choice == "always")
    assert "always" in always_words

    fake = FakeChatwork()
    a = make_adapter(fake, tmp_path)
    key = build_session_key(SessionSource(platform=a.platform, chat_id=TEAM, chat_type="group",
                                          user_id=str(HUMAN), user_name="山田"))

    async def handler(choice):
        return None

    async def scenario():
        await start(a)
        slash_confirm.register(key, "c1", "/reset", handler)
        try:
            sent = ["always", "!always", "!remember", " !Always "] + [f"!{w}" for w in always_words]
            mids = [_ask(fake, TEAM, w) for w in sent]
            plain_yes = _ask(fake, TEAM, "yes")  # "once" for their own /reset: allowed
            await a._poll_once()
        finally:
            slash_confirm.clear(key)
        return mids, plain_yes

    mids, plain_yes = run(scenario())
    assert [e.message_id for e in a.received] == [plain_yes]
    assert [mid for _, body, _ in fake.posted for mid in mids if f"-{mid}]" in body] == mids
