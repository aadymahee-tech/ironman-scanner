# Telegram bot

Push-only. The scheduled run calls the Bot API `sendMessage` method once per
message; there is no polling, webhook or command handling, so nothing has to
stay online. HTML parse mode, chunking at 4096 characters, `Retry-After`
handling for 429 and a plain-text fallback if Telegram rejects the HTML are in
`tradetron_pnl/telegram.py`.

## Create the bot and find the chat id

1. In Telegram open **@BotFather** → `/newbot` → choose a name and a
   username → copy the token (`123456789:AAH…`). Store it as the
   `TELEGRAM_BOT_TOKEN` secret.
2. Decide where messages go:
   * **Your own chat** — open the bot and press *Start* (a bot cannot message
     you first).
   * **A group** — add the bot to the group. Group ids are negative
     (`-100…` for supergroups).
   * **A channel** — add the bot as an administrator with *Post messages*.
3. Get the id. Send any message in the target chat, then:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python -m json.tool | grep -A3 '"chat"'
   ```

   Use the `"id"` of the right chat as `TELEGRAM_CHAT_ID`. For channels
   `@channelusername` also works for public channels.
4. Test from your machine (with `.env` filled in) or via *Run workflow*:

   ```bash
   python ttpnl.py send
   python ttpnl.py send --text "<b>bold</b> works, &lt;angle brackets&gt; must be escaped"
   ```

Exit code 3 means the bot could not deliver; the message is printed in the log
(`::warning title=Telegram::…`) and the data commit still happens.

## Messages

Times are IST. Positive/negative amounts carry ▲/▼ and are grouped the Indian
way (`₹1,23,456`). Names longer than 22 characters are shortened; use `alias`
in `config/deployments.csv` for nicer labels.

### 📊 Hourly (10:00–15:00)

```
📊 Tradetron · 12:00 IST
Today +₹1,300 ▲  (+₹450 since last update)
3 active · 1 in error ⚠️ · 4 open positions

• BN Condor  +₹300 (+₹100/h)  Active
• Crude Momentum  ₹0  ⚠️ Error-Execution
• Nifty Straddle  +₹1,000 (+₹350/h)  Live-Entered
paper (1): +₹510 — not counted

MTD +₹4,550 (6d, 50% win) · FYTD +₹4,550 (6d, 50% win)
```

`ˣ` after an amount marks a deployment whose day figure is partial (first seen
mid-day without a baseline).

### 🧾 End of day (15:40)

```
🧾 EOD · Fri 16 Oct 2026
Day +₹1,450 ▲
Streak: +2 days green
1 deployment(s) ended in error ⚠️

• BN Condor  -₹300
• Crude Momentum  ₹0
• Nifty Straddle  +₹1,750
paper (1): +₹510 — not counted

Week +₹3,250 (5d, 60% win)
MTD +₹3,250 (5d, 60% win) · FYTD +₹3,250 (5d, 60% win)
FY max drawdown -₹2,500 · profit factor 2.08
🧾 Advance tax 3rd instalment (75% cumulative) due 15 Dec — in 5 days. FYTD +₹3,250
```

The streak line appears only on non-flat days. The advance-tax reminder runs
from 21 days before each due date in the EOD message and from 7 days before in
the hourly one. Ledger notes for the day (reset detected, partial baseline,
positions hidden) are appended in italics.

### 📅 Weekly / 🗓 Monthly / 📆 Quarter

Sent together with the EOD message on the last trading day of the week, month
and FY quarter (holidays considered: if Friday is a holiday, Thursday's EOD
carries the weekly digest). Weekly shows a day-by-day list with best/worst
day; monthly a per-day calendar and the top/bottom deployments; quarter the FY
quarter totals and the matching advance-tax instalment.

### 🌅 Pre-open (09:00, optional)

Off by default (`TTPNL_PREOPEN_MESSAGE=1` to enable): count of active
deployments, how many are already in error before the session, and the
month / FY-to-date lines. The 09:00 run always stores the baseline snapshot
regardless of this setting.

### 🚨 Alerts

* **Deployment error** — any deployment whose status starts with `Error…`,
  once per deployment per day, in every run kind including silent ones.
* **Fetch failure** — when Tradetron cannot be read, once per day, with the
  error code and a hint (`AUTH` → check the key, `NOT_FOUND` → check the base
  URL, `RATE_LIMITED` → lower `TTPNL_MAX_POSITIONS`).

All messages are delivered with a normal notification. Set the repository
variable `TTPNL_SILENT_HOURLY=1` to deliver the hourly (and pre-open) updates
with `disable_notification` — they still arrive, without a sound — while EOD,
digests and alerts stay loud. Edit `tradetron_pnl/messages.py` to change
wording; every template has a test in `tests/test_messages.py`.

## Rate limits and failures

Telegram allows roughly one message per second per chat; a run sends at most
four (alert, EOD, weekly, monthly/quarter). HTTP 429 is retried after the
`retry_after` Telegram returns; network errors get two retries; an HTML
parse error (`400 … can't parse entities`) resends the same text without
`parse_mode` so the information still arrives. Other 400s (wrong chat id, bot
blocked, not an admin of the channel) raise immediately with the token
redacted.
