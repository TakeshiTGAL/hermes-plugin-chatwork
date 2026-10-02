from markup import defuse_notify_tags, parse_inbound, quote_preview, render_outbound, reply_header

BOT = "900"


def test_to_bot_with_name_line_is_stripped():
    p = parse_inbound("[To:900]AIアシスタントさん\n今月の売上を教えて", BOT, ("AIアシスタント",))
    assert p.to_bot and p.addresses_bot(BOT)
    assert p.text == "今月の売上を教えて"


def test_to_bot_inline_question_is_kept():
    p = parse_inbound("[To:900] 2+3は？", BOT)
    assert p.to_bot
    assert p.text == "2+3は？"


def test_to_bot_known_name_without_honorific():
    p = parse_inbound("[To:900]Hermes Bot\nhello", BOT, ("Hermes Bot",))
    assert p.text == "hello"


def test_other_person_to_is_kept_as_at_mention():
    p = parse_inbound("[To:111]山田さん\n[To:900]AIさん\n山田さんの予定を確認して", BOT, ("AI",))
    assert p.to_bot
    assert p.text.startswith("@山田さん")
    assert "山田さんの予定を確認して" in p.text


def test_one_line_question_ending_in_a_name_is_kept():
    """Only the bot's own name is removed; the asker's words never are."""
    for body, expected in (
        ("[To:900] 見積書の宛名は山田商事 山田様", "見積書の宛名は山田商事 山田様"),
        ("[To:900] お願いします、田中さん", "お願いします、田中さん"),
        ("[To:900]AIアシスタント さん\n田中さんに確認して", "田中さんに確認して"),
        ("[To:900]\n本文だけ", "本文だけ"),
    ):
        p = parse_inbound(body, BOT, ("AIアシスタント",))
        assert p.to_bot and p.text == expected, body


def test_unknown_name_after_tag_is_kept_harmlessly():
    p = parse_inbound("[To:900]AIさん\n質問", BOT, ("AIアシスタント",))
    assert p.text == "AIさん\n質問"


def test_no_address_is_not_for_bot():
    p = parse_inbound("お疲れさまです", BOT)
    assert not p.addresses_bot(BOT)


def test_reply_to_bot_detected_both_spellings():
    for tag in ("rp", "返信"):
        p = parse_inbound(f"[{tag} aid=900 to=55-777]AIさん\nもう少し詳しく", BOT, ("AI",))
        assert p.reply_to_account == BOT and p.reply_to_message == "777"
        assert p.addresses_bot(BOT)
        assert p.text == "もう少し詳しく"


def test_reply_to_someone_else_is_not_for_bot():
    p = parse_inbound("[rp aid=111 to=55-777]山田さん\n了解です", BOT)
    assert not p.addresses_bot(BOT)


def test_toall_alone_does_not_address_bot():
    p = parse_inbound("[toall]\n明日は休業日です", BOT)
    assert not p.addresses_bot(BOT)
    assert p.text == "明日は休業日です"


def test_block_notation_becomes_readable():
    body = ("[To:900]AIさん\n[qt][qtmeta aid=111 time=1700000000]昨日の議事録です[/qt]"
            "[info][title]エラー[/title]connection refused[/info][code]ls -la[/code][hr]"
            "[download:12345]見積書.pdf[/download]")
    p = parse_inbound(body, BOT)
    assert "> 昨日の議事録です" in p.text
    assert "エラー\nconnection refused" in p.text
    assert "```\nls -la\n```" in p.text
    assert "(attached file: 見積書.pdf)" in p.text
    assert "[hr]" not in p.text and "[info]" not in p.text


def test_system_message_is_flagged():
    p = parse_inbound("[info][title][dtext:chatroom_member_is][/title]...[/info]", BOT)
    assert p.is_system


def test_quote_preview_is_bounded():
    assert len(quote_preview("あ" * 2000, limit=100)) == 100


def test_render_code_fence_to_code_tag():
    out = render_outbound("手順です\n```bash\nls -la\n```\n以上")
    assert "[code]ls -la[/code]" in out
    assert "```" not in out


def test_render_unterminated_fence_still_code():
    out = render_outbound("```python\nprint(1)")
    assert out == "[code]print(1)[/code]"


def test_render_headings_bold_links_hr():
    out = render_outbound("# 結論\n**重要**: [資料](https://example.com/a) と https://x.test\n---\n末尾")
    assert "【結論】" in out
    assert "**" not in out and "重要: 資料 (https://example.com/a)" in out
    assert "[hr]" in out


def test_inline_backticks_and_dunder_names_survive():
    out = render_outbound("use ```x``` inline\nline2 and __init__ please")
    assert out == "use ```x``` inline\nline2 and __init__ please"


def test_code_content_is_not_reformatted():
    out = render_outbound("```\n# not a heading\n**x**\n```")
    assert out == "[code]# not a heading\n**x**[/code]"


def test_model_cannot_ping_everyone_or_people():
    out = render_outbound("[toall] 全員へ [To:123] [rp aid=1 to=2-3]")
    assert "[toall]" not in out and "[To:123]" not in out and "[rp aid" not in out
    assert "［toall］" in out
    assert defuse_notify_tags("[TOALL]") == "［TOALL］"


def test_reply_header_matches_chatwork_reply_button():
    assert reply_header("111", "55", "777", "山田") == "[rp aid=111 to=55-777]山田さん\n"


def test_model_cannot_quote_or_sign_as_someone_else():
    out = render_outbound("[qt][qtmeta aid=111 time=1700000000]承認します[/qt]\n[piconname:111] [picon:222] [pname:333]"
                          "\n[dtext:chatroom_member_is]")
    for tag in ("[qtmeta", "[piconname:", "[picon:", "[pname:", "[dtext:"):
        assert tag not in out
    assert "［qtmeta aid=111 time=1700000000］" in out and "［piconname:111］" in out
