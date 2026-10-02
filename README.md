# Chatwork for Hermes Agent

Put [Hermes Agent](https://github.com/NousResearch/hermes-agent) in a Chatwork room and ask it things the way you would ask a colleague: press **To** (or **Reply**) on its name, type the question, and the answer comes back as a Chatwork reply, with the usual notification.

```
山田:   [To] AIアシスタントさん
        来週の定例の議題を3つに絞って
AI:     [RE] 山田さん
        議題案です。
        ・先月の受注状況と今月の見込み
        ・新サービスの価格の決め方
        ・年末の休業日の連絡
```

- **No server or public URL.** The plugin checks Chatwork for new messages through the official API (every 5 seconds by default) and stays inside the API limit of 300 calls per 5 minutes.
- **Only speaks when spoken to.** In group rooms it answers `[To:]` and replies to its own messages, nothing else. Other conversation is never sent to the model.
- **Only in the rooms you list.** Mentions in any other room are ignored.
- **Never answers twice.** Its read position is saved, so a restart or crash does not produce duplicate answers.
- **Commands and approvals stay with you by default.** Anyone in a listed room can ask, but only the token's own account can use Hermes commands (beyond `/help`, `/whoami`, `/new`, `/reset`) or answer Hermes' approval prompts for risky actions until you choose who else may (see [Who can use it](#who-can-use-it-and-what-it-can-do)).

This is an unofficial, community plugin. It is not affiliated with or endorsed by the company that runs Chatwork, or by Nous Research.

日本語の説明は[下にあります](#日本語)。

## Before you start

- **Hermes Agent 0.21.4 or newer, already working.** `hermes --version` shows the version, `hermes update` upgrades it, and `hermes` on its own should answer you in the terminal. If it does not, set up Hermes first ([installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation)).
- **A model provider.** Hermes sends each question to the model you configured (OpenAI, Anthropic, OpenRouter, a local model, …). Usage is billed by that provider, not by this plugin.
- **A computer that stays on.** Hermes answers only while `hermes gateway` is running on your PC or server.
- **A Chatwork account for the bot** (next section).

## Make a bot account

A separate account makes it clear who is talking and keeps your own account's rooms private.

1. Sign up for a new Chatwork account with an address you control (for example `ai@your-company.co.jp`) and give it a clear name such as `AIアシスタント`.
2. From your usual account, add it as a contact and invite it to the room(s) where it should work. It must be allowed to write (not read-only).
3. Log in as the bot, open **Service integration → API token** ([direct link](https://www.chatwork.com/service/packages/chatwork/subpackages/api/token.php)) and issue a token.

If your company uses a Chatwork organization plan, an administrator may need to approve API use, and an extra account may count toward your user limit. Check with whoever manages Chatwork.

## Quick start

1. **Install.** It asks for two values and saves them to `~/.hermes/.env`: the bot account's API token, and the room id(s) where Hermes should work.

   ```bash
   hermes plugins install TakeshiTGAL/hermes-plugin-chatwork --enable
   ```

   Once the plugin is in the Hermes catalog, `hermes plugins install jp-chatwork --enable` does the same. From a local git clone: `hermes plugins install file:///path/to/hermes-plugin-chatwork --enable` (Hermes warns "Using insecure/local URL scheme" and installs it).

   The room id is the number after `rid` in the room's address when you open it in a browser (`https://www.chatwork.com/#!rid123456789` → `123456789`). Several rooms: `123456789,987654321`.

2. **Start.**

   ```bash
   hermes gateway run
   ```

   The log shows `Connected as AIアシスタント (account ...). Watching 1 room(s)`. If it says `Watching 0 room(s)`, the warning just above it says why.

3. **Ask.** In that room, press **To**, pick the bot and write your question. The answer arrives as a reply a few seconds after the model finishes.

4. **Choose a home room.** Hermes sends scheduled results and its own notices to a "home" room. The plugin does not pick one for you. A 1:1 chat between your usual account and the bot is a good choice: you see the notices and the team does not. Open that chat in a browser, copy the number after `rid`, then add `CHATWORK_HOME_CHANNEL=that-number` to `~/.hermes/.env` and restart. (`/sethome` in that chat does not work with a separate bot account unless your account is in `CHATWORK_ALLOWED_USERS`.) Trying it alone in My Chat? Use your My Chat id. Until a home is set, see `CHATWORK_HOME_CHANNEL` under [Settings](#settings) for what happens.

To change the token or rooms later, edit `CHATWORK_API_TOKEN=` / `CHATWORK_ROOMS=` in `~/.hermes/.env` and restart. To keep Hermes running in the background, use `hermes gateway install` instead of `hermes gateway run`.

**Recommended for a Japanese team room:** Hermes' own short messages (approval questions, "shutting down") are English by default, and tool progress is shown as extra messages. The first setting below switches Hermes' own messages to Japanese where Hermes has a translation; how many are translated depends on the Hermes version (on 0.21.4, for example, the approval question and the "shutting down" notice are still English, while newer versions translate them). The second turns the progress messages off (0.21.4 and later). The model's answers follow the language of the question either way:

```bash
hermes config set display.language ja
hermes config set display.platforms.chatwork.tool_progress off
```

## How it behaves

| Where | Hermes answers |
|---|---|
| Group room listed in `CHATWORK_ROOMS` | Messages with `[To:bot]`, or a **Reply** to one of its messages |
| 1:1 chat with the bot (listed) | Every message |
| Room listed in `CHATWORK_FREE_RESPONSE_ROOMS` | Every message, no `[To:]` needed |
| Any room not listed | Nothing |

- The answer starts with Chatwork's reply header, so the person who asked is notified once per question.
- Code and commands are shown in a Chatwork `[code]` box. Markdown the model writes (`**bold**`, `# headings`, links) is turned into plain text Chatwork can display. Answers longer than about 10,000 characters are split into numbered parts.
- When someone quotes (`[qt]`), uses `[info]` or `[code]`, the model receives readable text. When someone replies to a message, the model also sees that message.
- The model cannot ping people: `[toall]`, `[To:...]` and `[rp ...]` in its output are made harmless. Only the plugin adds the one reply header. It cannot quote someone with their name and icon either (`[qtmeta]`, `[piconname]` are made harmless).
- When Hermes needs approval for a risky action, it posts the question in the room. Who can answer it depends on `CHATWORK_ALLOWED_USERS` (next sections). Someone who may answer does so by **replying** to that message with `/approve` or `/deny`. If nobody answers in time (`approvals.timeout`, 300 seconds unless you changed it), the action does not run.
- Questions sent while Hermes was stopped are answered when it starts again, if they are less than 24 hours old (and among the last 100 messages of the room). Older ones are skipped and logged.
- Editing a question after sending it does not trigger a new answer. Send a new message instead.
- Removing a room from `CHATWORK_ROOMS` makes Hermes forget its read position. If the room is added back later, Hermes starts from that moment and does not answer what was said in between.

## Settings

Put these in `~/.hermes/.env` (or under `platforms.chatwork.extra` in `config.yaml`: `token`, `rooms`, `require_mention`, `free_response_rooms`, `poll_interval`, `notice_delivery`). The `private` default for notices applies when the token is in `~/.hermes/.env`; with the token only in `config.yaml`, add `notice_delivery: private` there.

| Variable | Default | Meaning |
|---|---|---|
| `CHATWORK_API_TOKEN` | (required) | Token of the account Hermes speaks as |
| `CHATWORK_ROOMS` | (required) | Room ids Hermes works in, comma-separated. If empty, Hermes answers nowhere and logs the rooms the account is in |
| `CHATWORK_REQUIRE_MENTION` | `true` | `false`: answer every message in the listed group rooms |
| `CHATWORK_FREE_RESPONSE_ROOMS` | empty | Rooms where every message is answered |
| `CHATWORK_POLL_INTERVAL` | `5` | Seconds between checks (minimum 2). API use: 1 call per check, plus 1 per room with new messages, plus 1 per listed room every 12th check (once a minute at the default 5 seconds) as a safety re-read. With 5 seconds and 3 rooms that is about 80 of the 300 calls per 5 minutes |
| `CHATWORK_ALLOWED_USERS` | empty | Only these account ids may use the bot, and they can use every Hermes command and answer approval prompts. Empty means everyone in the listed rooms may ask, but only the token's own account can use Hermes commands other than `/help`, `/whoami`, `/new` and `/reset`, or answer approvals |
| `CHATWORK_HOME_CHANNEL` | not set | Room for Hermes' own notices, scheduled jobs (`deliver=chatwork`) and `hermes send --to chatwork` without a room id. The plugin never picks one. While it is not set: notices meant for the home room (such as "shutting down") are not sent to Chatwork at all; the first question of each new conversation also triggers Hermes' one-time "No home channel is set ... /sethome" notice, which goes to the gateway log in a group room and to the chat in My Chat or a 1:1 chat (see `CHATWORK_NOTICE_DELIVERY`); scheduled jobs that were not created from a Chatwork room and `hermes send --to chatwork` without a room id are not delivered. A notice about a task that was running still goes to the chat where it was asked. `/sethome` in a room also sets it, but only for an account that may use Hermes commands (see below) |
| `CHATWORK_NOTICE_DELIVERY` | `private` | Hermes' notices meant for one person (the "No home channel" hint, a failed helper task, a sign-in prompt) are written to the gateway log instead of a group room, because Chatwork has no message only one member of a room can see. In My Chat and 1:1 chats they are posted as usual. `public`: post them in group rooms too |

Account and room ids are numbers. The gateway log shows the bot's id at start (`Connected as ... (account 1234567)`) and the asker's id next to every question it answers (`Question from 山田 (account 2345678) in room ...`), which is the easiest way to collect ids for `CHATWORK_ALLOWED_USERS`.

## Who can use it, and what it can do

Everyone in a listed room can ask Hermes things, including guests from other companies if the room has them. Hermes can do whatever its tools allow on the machine it runs on, and before a risky action (a dangerous shell command, for example) it stops and asks for approval.

**Commands and approvals, by default.** While `CHATWORK_ALLOWED_USERS` is not set, people other than the token's account can use only `/help`, `/whoami`, `/new` and `/reset`; every other Hermes command (`/approve`, `/deny`, `/yolo`, `/sethome`, `/pause`, `/model`, `/memory` and so on, and "restart gateway" typed in a 1:1 chat) and a bare `yes` / `ok` / 👍 while an approval is waiting (on newer Hermes with `display.language ja`, also words such as `はい`) get a reply explaining why and how to change it, and do nothing. Command-looking text that is not a Hermes command, such as a skill name, reaches the model as ordinary text and runs nothing. Their questions are handled as usual. Once you set `CHATWORK_ALLOWED_USERS`, the listed accounts can use every Hermes command, including `/sethome`, `/model --global` and `/approvals`, so list only people you trust with that machine. In practice:

- With a separate bot account in a group room or a 1:1 chat, nobody can approve: this plugin does not read the bot account's own messages there, and Hermes keeps each approval with the conversation of the person who asked. The question expires and the action does not run.
- In My Chat with your own token (see [Try it alone](#try-it-alone-in-my-chat)), you are the token's account, so you can use every command and answer your own approvals.
- `/sethome` follows the same rule. With a separate bot account and no `CHATWORK_ALLOWED_USERS`, nobody can use it in a room, so set the home room with `CHATWORK_HOME_CHANNEL` instead.
- To let people approve in shared rooms, set `CHATWORK_ALLOWED_USERS=1234567,2345678` (include your own usual account). Only those accounts can then talk to the bot at all, and each of them answers the approvals for their own questions, as on other Hermes platforms.

**Rooms with guests from other companies.** Guests can ask Hermes, and Hermes' answers can include what its tools can read on your machine. Before adding the bot to such a room:

- limit the tools for this platform: `hermes tools` → choose `chatwork`, and turn off what the room should not have (for example terminal and file tools);
- if you set `CHATWORK_ALLOWED_USERS` so people can approve or use commands, list only people you trust with that machine, never a guest;
- remember that what guests write to the bot is sent to your model provider.

This plugin never approves anything itself and never turns approvals off.

## Try it alone in My Chat

You can test without a bot account, using your own token and your **My Chat** (where you are the only member). Open My Chat in a browser; the number after `rid` is its id. Then set these three lines in `~/.hermes/.env` (replace the lines if they already exist):

```
CHATWORK_API_TOKEN=your-own-token
CHATWORK_ROOMS=your-my-chat-id
CHATWORK_FREE_RESPONSE_ROOMS=your-my-chat-id
```

and run `hermes gateway run`. Every message you write in My Chat is then a question, and the answers are posted under your name. Hermes recognises its own answers, so it does not reply to itself. Do not list team rooms with a personal token: Hermes would be answering as you.

## When something goes wrong

The gateway log (`hermes gateway run` output, or `~/.hermes/logs/gateway.log`) says what happened and what to do:

| Log message contains | What to do |
|---|---|
| `Chatwork rejected the API token (401` | Issue a new token as the bot account, update `CHATWORK_API_TOKEN`, restart |
| `CHATWORK_API_TOKEN is not set` | Add the token to `~/.hermes/.env` |
| `CHATWORK_ROOMS is empty` | The log lists the rooms the account is in; add the right ids |
| `is in CHATWORK_ROOMS but the account ... is not a member` | Invite the bot account to that room, or remove the id |
| `The account cannot post in room` | Make the bot a member who can write, not read-only |
| `rate limited` / `rate limit still exceeded` | Nothing; it waits and continues. If it is frequent, raise `CHATWORK_POLL_INTERVAL` |
| `This Chatwork API token already in use` | Another Hermes gateway on this computer uses the same token. Stop it, or give each profile its own bot account |
| `Skipped message ... hours old` | A question older than 24 hours was not answered. Ask again |

Network failures and Chatwork outages are retried with increasing waits; the Chatwork connection keeps running. It stops only when the token is missing or rejected, because waiting cannot fix that. If Chatwork is the only platform in this Hermes, the gateway then exits with that message; with other platforms, they keep running.

## Stop or remove

- Stop: `Ctrl+C` in the window running `hermes gateway run`, or `hermes gateway stop` if you used `hermes gateway install`.
- Remove the plugin: `hermes plugins remove jp-chatwork`, then delete the `CHATWORK_*` lines from `~/.hermes/.env`.
- Revoke access: log in as the bot and delete or reissue the token on the API token page. The read position file (below) can be deleted.

## What it sends where (disclosure)

- **Network:** only `https://api.chatwork.com/v2`, with the token from `CHATWORK_API_TOKEN` in the `x-chatworktoken` header. Messages in the listed rooms that are addressed to the bot (and the message they reply to) are passed to your configured model provider, like any other Hermes platform.
- **Background work:** while the gateway runs, one task checks the listed rooms every `CHATWORK_POLL_INTERVAL` seconds (API use is in the `CHATWORK_POLL_INTERVAL` row above).
- **Files:** `~/.hermes/plugin-data/jp-chatwork/state.json` holds the bot's account id, the newest handled message id per room, and the ids of the last 1,000 messages the bot posted. The gateway log records the name and account id of each asker. No telemetry.
- **Token:** `hermes plugins install` asks for it (masked) and Hermes saves it in `~/.hermes/.env`. The plugin reads it from `CHATWORK_API_TOKEN`, sends it only to `api.chatwork.com`, and never writes or logs it. Hermes' one-gateway-per-token lock file keeps a 16-character SHA-256 prefix of it, not the token.
- **Dependencies:** none beyond Hermes (it uses `httpx`, which Hermes already ships).

## Limits

- Text only. Attached files and images are shown to the model as a file name, not read.
- Polling, not webhooks: answers start up to `CHATWORK_POLL_INTERVAL` seconds after the question.
- If a room receives more than 100 messages between two checks, the oldest of them are skipped (and logged).
- Answers arrive in one piece when finished; they are not typed out live.
- How it was tested: the full round trip (question → Hermes → reply with the Chatwork reply header, restarts without duplicate answers, rejected token, unlisted room) was run against the real Chatwork API by one person alone in a single account's My Chat. A round trip from a different account in a group room has not been demonstrated. That behaviour (addressing, reply headers, who may ask, use commands and answer approvals) is covered by the offline tests only.

## Development

```bash
git clone https://github.com/TakeshiTGAL/hermes-plugin-chatwork && cd hermes-plugin-chatwork
uv pip install -p /path/to/hermes-agent/.venv/bin/python pytest   # once; Hermes does not ship pytest
/path/to/hermes-agent/.venv/bin/python -m pytest -q               # offline; no token needed
hermes plugins validate . --install-deps                         # what the catalog CI runs
```

Files: `adapter.py` (gateway adapter and registration), `client.py` (Chatwork API and rate limits), `markup.py` (Chatwork notation ↔ text), `state.py` (read position). The offline tests pass with Hermes 0.21.4 and with Hermes main at commit 0a374d16 (2026-10-02).

License: MIT.

---

## 日本語

Chatwork のルームに AI 担当（Hermes Agent）を置くためのプラグインです。同僚に頼むときと同じように、**To** か**返信**で話しかけると、Chatwork の返信として答えが届きます。

### できること

- 会議の議題づくり、文面の下書き、調べもの、手順の説明などを、いつものルームの中で頼めます。
- 話しかけられたときだけ答えます。グループチャットでは、AI への To か、AI の発言への返信にだけ反応します。ほかの会話は AI に送られません。
- 動かすルームは自分で決めます。指定していないルームでは、To されても反応しません。
- サーバーや公開 URL は要りません。Hermes を動かしているパソコンから、Chatwork の API に数秒おきに新着を確かめに行きます（API の上限「5 分で 300 回」の範囲内）。
- 再起動しても、同じメッセージに二度答えることはありません。
- Hermes のコマンドと危ない操作の承認は、最初はトークンのアカウント本人しか使えません。誰に任せるかは自分で決めます（「安全のために」）。

非公式のプラグインです。Chatwork を運営する会社とも、Nous Research とも関係はありません。

### 始める前に

- **Hermes Agent 0.21.4 以降が動いていること。** `hermes --version` で版が分かり、`hermes update` で更新できます。ターミナルで `hermes` と打って AI が答えれば準備できています。まだなら先に Hermes を入れてください（[導入手順・英語](https://hermes-agent.nousresearch.com/docs/getting-started/installation)）。
- **AI の契約。** 質問は、Hermes に設定した AI（OpenAI、Anthropic、OpenRouter、手元のモデルなど）へ送られます。利用料はその AI の会社から請求されます。このプラグイン自体は無料です。
- **つけっぱなしのパソコンかサーバー。** `hermes gateway` が動いている間だけ答えます。
- **ボット用の Chatwork アカウント。**（次の節）

### ボット用アカウントの作り方

1. 会社で受け取れるメールアドレス（例: `ai@自社ドメイン`）で Chatwork のアカウントを新しく作り、名前を「AIアシスタント」など分かりやすくします。
2. ふだんのアカウントからコンタクトに追加し、使いたいルームに招待します。閲覧のみではなく、書き込みできる権限にします。
3. ボット用アカウントでログインし、「サービス連携」→「API トークン」（[直接開く](https://www.chatwork.com/service/packages/chatwork/subpackages/api/token.php)）でトークンを発行します。

組織プランの場合は、管理者による API 利用の承認が要ることがあります。また、アカウントを 1 つ増やすと利用人数に数えられることがあります。社内の Chatwork 管理者に確認してください。

### 始め方（4 手）

1. **入れる**: 次のコマンドを打つと、ボット用アカウントの API トークンと、AI を置くルームの ID を聞かれます。答えると `~/.hermes/.env` に保存されます。

   ```bash
   hermes plugins install TakeshiTGAL/hermes-plugin-chatwork --enable
   ```

   Hermes のカタログに載ったあとは `hermes plugins install jp-chatwork --enable` でも入ります。手元に git clone したものから入れるときは `hermes plugins install file:///path/to/hermes-plugin-chatwork --enable` です（「Using insecure/local URL scheme」という警告が出ますが、入ります）。

   ルーム ID は、ブラウザでそのルームを開いたときのアドレスの `rid` の後ろの数字です（`https://www.chatwork.com/#!rid123456789` なら `123456789`）。複数あるときは `,` でつなぎます。

2. **動かす**:

   ```bash
   hermes gateway run
   ```

   画面に `Connected as AIアシスタント (account ...). Watching 1 room(s)` と出れば準備完了です。`Watching 0 room(s)` のときは、そのすぐ上の行に理由が出ています。

3. **頼む**: そのルームで **To** を押して AI を選び、頼みたいことを書きます。少し待つと返信で答えが届きます。

4. **ホームのルームを決める**: Hermes は、定期実行の結果や Hermes 自身のお知らせを「ホーム」のルームへ送ります。プラグインは勝手に決めません。おすすめは、ふだんの自分のアカウントとボットの 1 対 1 のチャットです。お知らせは自分に届き、チームのルームには流れません。ブラウザでそのチャットを開き、アドレスの `rid` の後ろの数字を控えて、`~/.hermes/.env` に `CHATWORK_HOME_CHANNEL=その数字` を書いて再起動します（ボット用アカウントのときは、自分のアカウントを `CHATWORK_ALLOWED_USERS` に入れていない限り、そのチャットで `/sethome` と書いても設定できません）。マイチャットで一人で試すときは、マイチャットの ID にします。決めないときにどうなるかは「設定の一覧」の `CHATWORK_HOME_CHANNEL` にあります。

あとから変えるときは `~/.hermes/.env` の `CHATWORK_API_TOKEN=...` と `CHATWORK_ROOMS=...` を書き換えて、もう一度起動します。常に動かしておくなら `hermes gateway run` の代わりに `hermes gateway install` を使います。

**日本のチームで使うなら、次の 2 つも設定してください。** 1 つ目は、Hermes 自身の短いお知らせ（承認の確認や「停止します」など）を、Hermes に訳があるものから日本語にします。どこまで日本語になるかは Hermes の版によります（たとえば 0.21.4 では承認の確認と「停止します」は英語のままで、新しい版では日本語になります）。2 つ目は作業途中の経過メッセージを止めます（0.21.4 以降）。AI の答えそのものは、設定に関係なく質問と同じ言葉で返ります。

```bash
hermes config set display.language ja
hermes config set display.platforms.chatwork.tool_progress off
```

### ふるまい

| 場所 | AI が答えるもの |
|---|---|
| `CHATWORK_ROOMS` に入れたグループチャット | AI への To、または AI の発言への返信 |
| AI との 1 対 1 のチャット（指定したもの） | すべてのメッセージ |
| `CHATWORK_FREE_RESPONSE_ROOMS` に入れたルーム | すべてのメッセージ（To 不要） |
| 指定していないルーム | 何もしない |

- 答えは返信の形で届き、質問した人に通知が 1 回届きます。
- コードやコマンドは Chatwork の `[code]` の枠で表示します。長い答え（1 万字ほど以上）は分けて投稿します。
- 引用（`[qt]`）や `[info]` もそのまま読めます。返信で質問したときは、元のメッセージも AI に渡します。
- AI の答えに `[toall]` や `[To:...]` が入っても、通知は飛びません。ほかの人の名前とアイコンを付けた引用（`[qtmeta]`、`[piconname]` など）は出せないようにしてあります。
- 危ない操作の前に、AI が承認を求めるメッセージを出すことがあります。誰が答えられるかは `CHATWORK_ALLOWED_USERS` で決まります（「安全のために」）。答えられる人は、そのメッセージに**返信**で `/approve`（許可）か `/deny`（拒否）と書きます。時間内（`approvals.timeout`、変えていなければ 300 秒）に誰も答えなければ、その操作は実行されません。
- Hermes を止めている間に来た質問は、24 時間以内のものなら次に起動したときに答えます。それより古いものは答えず、ログに残します。
- 送ったあとで質問を編集しても、答え直しはしません。新しく書き直してください。
- `CHATWORK_ROOMS` からルームを外すと、そのルームの読み取り位置を忘れます。あとで戻したときは、その時点から先だけに答えます（外していた間の質問には答えません）。

### 設定の一覧

`~/.hermes/.env` に書きます。トークンを `config.yaml` だけに書いているときは、お知らせの既定 `private` が効かないので、`platforms.chatwork.extra` に `notice_delivery: private` も書きます。

| 名前 | 決めないとき | 意味 |
|---|---|---|
| `CHATWORK_API_TOKEN` | （必須） | AI として話すアカウントの API トークン |
| `CHATWORK_ROOMS` | （必須） | AI を置くルーム ID（`,` 区切り）。空なら、どこでも答えず、入っているルームの一覧をログに出します |
| `CHATWORK_REQUIRE_MENTION` | `true` | `false` にすると、指定したグループチャットのすべての発言に答えます |
| `CHATWORK_FREE_RESPONSE_ROOMS` | 空 | To なしで全部に答えるルーム |
| `CHATWORK_POLL_INTERVAL` | `5` | 新着を確かめる間隔（秒、最小 2）。API の回数は、確かめるたびに 1 回、新着のあったルームごとに 1 回、さらに念のため 12 回に 1 回（既定の 5 秒なら 1 分に 1 回）、指定ルームを 1 回ずつ読み直します。5 秒・3 ルームなら、上限「5 分で 300 回」のうち約 80 回です |
| `CHATWORK_ALLOWED_USERS` | 空 | 使える人をアカウント ID で限ります。ここに入れた人は、Hermes のコマンドをすべて使え、承認にも答えられます。空なら指定ルームの全員が頼めますが、`/help`・`/whoami`・`/new`・`/reset` 以外の Hermes のコマンドと承認は、トークンのアカウント本人しか使えません |
| `CHATWORK_HOME_CHANNEL` | 未設定 | Hermes 自身のお知らせ、定期実行の結果（`deliver=chatwork`）、ルーム ID を付けない `hermes send --to chatwork` の送り先。プラグインは勝手に決めません。未設定の間は、ホームに送るお知らせ（「停止します」など）は Chatwork のどこにも届きません。新しい会話の最初の質問のときに、Hermes が一度だけ「No home channel is set … /sethome」の案内を出します。グループチャットではルームに出さずに Hermes のログへ書き、マイチャットと 1 対 1 のチャットではそのチャットに出します（`CHATWORK_NOTICE_DELIVERY` を参照）。Chatwork のルームから作っていない定期実行と、ルーム ID を付けない `hermes send --to chatwork` は届きません。実行中だった作業についてのお知らせは、質問されたルームに届きます。ルームで `/sethome` と書いても設定できますが、Hermes のコマンドを使える人に限ります（「安全のために」） |
| `CHATWORK_NOTICE_DELIVERY` | `private` | 一人あてのお知らせ（「No home channel」の案内、補助の作業の失敗、ログインの案内など）を、グループチャットには出さず Hermes のログへ書きます。Chatwork には、ルームの一人にだけ見えるメッセージが無いためです。マイチャットと 1 対 1 のチャットには、ふだんどおり出します。`public` にするとグループチャットにも出します |

アカウント ID は、AI が答えるたびにログへ出る `Question from 山田 (account 2345678)` で分かります。

### 安全のために

指定したルームにいる人は全員、AI に頼みごとができます。社外のゲストがいるルームも同じです。AI は、Hermes を動かしているパソコンで、使える道具の範囲のことができます。危ない操作（危険なコマンドなど）の前には、止まって承認を求めます。

**コマンドと承認の既定。** `CHATWORK_ALLOWED_USERS` を設定していない間は、トークンのアカウント以外の人が使えるコマンドは `/help`・`/whoami`・`/new`・`/reset` だけです。ほかの Hermes のコマンド（`/approve`・`/deny`・`/yolo`・`/sethome`・`/pause`・`/model`・`/memory` など。1 対 1 のチャットで書いた「restart gateway」も含みます）や、承認待ちの間に `yes`・`ok`・👍 とだけ書いたもの（新しい版の Hermes で `display.language ja` なら「はい」なども）には、理由と設定のしかたを返信し、何も実行しません。スキル名など、Hermes のコマンドではない「/」で始まる文は、ふつうの文として AI に届き、何も実行しません。その人の質問は、ふだんどおり AI に届きます。`CHATWORK_ALLOWED_USERS` を設定すると、ここに入れた人は `/sethome`・`/model --global`・`/approvals` も含めて Hermes のコマンドをすべて使えます。そのパソコンを任せられる人だけを入れてください。実際には次のようになります。

- ボット用アカウントをグループチャットや 1 対 1 のチャットで使うときは、誰も承認できません。このプラグインはそこでボット自身の発言を読まず、Hermes は承認を質問した人の会話にひも付けるからです。承認は時間切れになり、その操作は実行されません。
- 自分のトークンでマイチャットを使うとき（「一人で試す」）は、あなたがトークンのアカウント本人なので、すべてのコマンドを使え、自分の承認に自分で答えられます。
- `/sethome` も同じです。ボット用アカウントで `CHATWORK_ALLOWED_USERS` を設定していないときは、ルームでは誰も使えないので、ホームのルームは `CHATWORK_HOME_CHANNEL` で決めます。
- 共有ルームで承認できる人を決めるには、`CHATWORK_ALLOWED_USERS=1234567,2345678` を設定します（ふだん使う自分のアカウントも入れます）。そのときは、ここに入れた人しか AI と話せなくなり、それぞれが自分の質問の承認に答えます。ほかの Hermes の接続先と同じ動きです。

**社外のゲストがいるルーム。** ゲストも AI に頼めます。AI の答えには、道具で読めるパソコンの中身が入ることがあります。そのようなルームに入れる前に、次のことをしてください。

- AI が使える道具（ファイル操作やコマンド実行など）を、`hermes tools` で Chatwork 用に絞ります。要らない道具は外します。
- 承認やコマンドを任せるために `CHATWORK_ALLOWED_USERS` を設定するときは、そのパソコンを任せられる人だけを入れます。ゲストは入れません。
- ゲストが AI に書いたことも、Hermes に設定した AI の会社へ送られます。

このプラグインが自分で承認したり、承認を止めたりすることはありません。

### どこへ何を送り、何を残すか

- 通信先は Chatwork の API（`https://api.chatwork.com/v2`）だけです。
- 指定したルームで AI に話しかけた発言（と、返信元のメッセージ）は、Hermes に設定した AI の会社へ送られます。海外の会社のこともあります。社外秘を扱うなら、その会社の規約を確認してください。
- パソコンに残すのは、どこまで読んだかの記録（`~/.hermes/plugin-data/jp-chatwork/state.json`。ボットのアカウント ID、ルームごとに処理済みの最新のメッセージ ID、ボットが投稿した直近 1,000 件のメッセージ ID）と、Hermes のログ（質問した人の名前とアカウント ID を含む）だけです。利用状況をどこかへ送る機能はありません。
- API トークンは、`hermes plugins install` のときに伏せ字で聞かれ、Hermes が `~/.hermes/.env` に保存します。プラグインは `CHATWORK_API_TOKEN` から読み、`api.chatwork.com` にだけ送り、ファイルやログには書きません。同じトークンで Hermes が二重に動かないための Hermes のロックファイルには、トークンそのものではなく、SHA-256 の先頭 16 文字だけが残ります。

### 困ったとき

起動した画面（または `~/.hermes/logs/gateway.log`）に、何をすればよいかが英語で出ます。主なものは次のとおりです。

- `Chatwork rejected the API token (401` … トークンが違うか、作り直されています。ボット用アカウントで発行し直し、`CHATWORK_API_TOKEN` を書き換えて再起動します。
- `CHATWORK_API_TOKEN is not set` … トークンが設定されていません。`~/.hermes/.env` に書きます。
- `CHATWORK_ROOMS is empty` … ルームが指定されていません。ボットが入っているルームの一覧がログに出るので、その ID を指定します。
- `is not a member` … ボット用アカウントがそのルームにいません。招待するか、ID を外します。
- `The account cannot post in room` … 閲覧のみになっています。書き込みできる権限にします。
- `rate limited` … API の回数制限です。自動で待って再開するので、操作は要りません。頻繁なら `CHATWORK_POLL_INTERVAL` を大きくします。
- `already in use` … 同じトークンで、別の Hermes がこのパソコンで動いています。どちらかを止めます。
- `Skipped message ... hours old` … 24 時間より古い質問には答えません。もう一度書いてください。

ネットワークが切れても、Chatwork 側が止まっても、間隔を空けながら自動で再開します。止まるのは、トークンが無いときと拒否されたときだけです。Hermes で Chatwork しか使っていなければ、そのときは Hermes ごと終了します。

### 一人で試す（マイチャット）

ボット用アカウントを作る前に、自分のトークンとマイチャットで試せます。ブラウザでマイチャットを開き、アドレスの `rid` の後ろの数字を控えます。`~/.hermes/.env` を次の 3 行にします（同じ名前の行があれば書き換えます）。

```
CHATWORK_API_TOKEN=自分のAPIトークン
CHATWORK_ROOMS=マイチャットのID
CHATWORK_FREE_RESPONSE_ROOMS=マイチャットのID
```

`hermes gateway run` で起動すると、マイチャットに書いたことがそのまま質問になり、答えは自分の名前で投稿されます。AI は自分の答えには反応しません。自分のトークンのまま、チームのルームを指定しないでください（AI があなたの名前で答えてしまいます）。

### できないこと・確かめた範囲

- 文字だけを扱います。添付ファイルや画像は、ファイル名だけが AI に伝わり、中身は読みません。
- 新着は数秒おきに確かめに行く方式なので、質問してから答え始めるまで最大 `CHATWORK_POLL_INTERVAL` 秒かかります。
- 1 回の確認の間に 100 件を超える発言があったルームでは、古いほうを読み飛ばします（ログに残します）。
- 答えはできあがってから 1 通で届きます。少しずつ表示されることはありません。
- 確かめた範囲: 本物の Chatwork API を使い、1 つのアカウントのマイチャットで一人で、質問 → Hermes → 返信つきの答え、再起動しても二重に答えないこと、拒否されたトークン、指定していないルームを確かめました。グループチャットで、別のアカウントから話しかけての往復は実演していません。その動き（宛先の判定、返信の形、使える人・コマンドを使える人・承認できる人の判定）は、手元のテストだけで確かめています。

### 止める・外す

- 止める: `hermes gateway run` の画面で `Ctrl+C`。`hermes gateway install` で常駐させたときは `hermes gateway stop`。
- 外す: `hermes plugins remove jp-chatwork` を打ち、`~/.hermes/.env` の `CHATWORK_` で始まる行を消します。
- トークンを無効にする: ボット用アカウントで API トークンの画面を開き、削除するか発行し直します。
