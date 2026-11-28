# Zalo Order Assistant

Reads the shop's Zalo group chat, picks out the pre-order messages ("mai cho chị 2 phần"), and saves each one as an order in the Bếp Phương Bắc Supabase app: customer name, number of portions, delivery date and the original message. Staff then see the orders per day in the app, as they do today.

- The chat is read through Zalo Web in a Chrome window, read-only. It never sends anything to Zalo.
- Messages are understood by a **local** Qwen model (Ollama). Chat content only goes to your own Supabase project.
- The delivery date is worked out in code from day words ("mai", "thứ sáu", "28/11", ...) relative to the day the message was written, not from the model.

Design and requirements: [spec.md](spec.md).

## 1. Setup (once)

You need a Mac or Linux machine that stays on while the shop takes orders.

1. **Python tools**: install [uv](https://docs.astral.sh/uv/) (`brew install uv`).
2. **Browser automation**: install [`playwright-cli`](https://github.com/microsoft/playwright-cli) and check that `playwright-cli --help` works.
3. **Local model**: install [Ollama](https://ollama.com), then:
   ```bash
   ollama pull qwen2.5:7b-instruct
   ```
4. **Project**:
   ```bash
   cd zalo-order-assistant
   uv sync
   cp .env.example .env
   ```
5. **Edit `.env`** and set at least:
   - `SUPABASE_URL`: the Supabase project URL of the Bếp Phương Bắc app.
   - `SUPABASE_API_KEY`: an API key for its `orders` function (see the bep-phuong-bac-supabase README). Never commit `.env` or share this key.
   - `ZALO_CHAT`: the exact title of the shop's **group** chat, for example `Bếp Phương Bắc - Đặt cơm`. Case and accents do not matter, but every word must be there.

## 2. Start and stop

```bash
./start.sh
```

`start.sh` checks that `.env`, Ollama and `playwright-cli` are ready, then runs `uv run zalo-order-assistant`.

The first time, a Chrome window opens on Zalo Web. **Scan the QR code with the shop's phone.** The login is kept in `data/browser-profile`, so you only do this once. The assistant then opens the group chat itself and reads the last `BACKFILL_DAYS` days (default 3). Leave the window open; you can minimise it.

Stop with **Ctrl+C**. Nothing is lost: a message that was being processed is picked up again on the next start.

### The status line

Each time something changes, the console prints a line like:

```
[status] reader=READY pipeline=OK queued=0
```

- `reader` is what the Zalo side is doing (table below).
- `pipeline` is whether orders can be saved (table below).
- `queued` is the number of messages waiting to be processed.

## 3. What it does with each message

| Message | Result |
|---|---|
| A pre-order ("mai lấy 2 phần", "đặt cơm", "thứ 6 cho e 1 suất") | Added to the customer's order for that day. No number means 1 portion. No day means the day the message was written. A second pre-order for the same day adds to the same order. |
| More portions ("thêm 1 suất") | Added to the order. |
| Fewer portions ("bớt 1 phần", "giảm 2 suất", "hủy 1 phần") | Taken off the order (never below 0). The number must be written: "bớt đi nhé" is ignored and noted. |
| A new total ("sửa lại 3 phần", "đổi thành 2 phần", "vẫn 2 phần nhé", "chốt 2 phần", "À nhầm 4s") | The order becomes that total. |
| A cancellation ("hủy đơn", "mai không lấy nữa") | The order goes to 0 and shows as cancelled. |
| A change when the customer has no open order that day (nothing ordered, or already delivered) | Nothing changes. Noted in the audit log as `not_applied`, and a WARNING `order_not_applied` is logged. |
| The same message read again (restart, backfill) | Nothing new: the backend recognises the message key. |
| Chat, questions, thanks, "đã nhận 2 phần" | Ignored, and noted in the audit log. |
| A past day ("hôm qua", "hôm kia", "tuần trước") | Ignored, noted. |
| Two different days in one message ("mai 2 phần, kia 1 phần") | Ignored, noted. Ask the customer to send one message per order. |
| An invalid date ("31/11") or one more than `MAX_DAYS_AHEAD` days ahead | Ignored, noted. |
| Your own account's messages (`INCLUDE_OWN_MESSAGES=false`) | Ignored, noted. Type orders for customers in the Supabase app instead. |
| Images, stickers, files | Skipped. |

A change with no day word ("bớt 1 phần") applies to the day the message was written. A customer who ordered "mai 2 phần" and writes "bớt 1 phần" the same evening should say "mai bớt 1 phần"; otherwise the change is `not_applied` until the backend learns to find the next open order (spec v2, B1).

The model can still be wrong on unusual wording. On the test set of 105 messages it was right every time, including every change message ([reports/extractor-accuracy-v2.md](reports/extractor-accuracy-v2.md)). If a customer says an order is missing, check the audit log (section 6).

## 4. Configuration

All settings go in `.env` (see [.env.example](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_API_KEY` | required | Orders API. The key is never logged. |
| `ZALO_CHAT` | empty | Title of the group chat to open. Empty = read the chat already open, which must be a group. |
| `BACKFILL_DAYS` | `3` | Days of history read when the chat is opened. |
| `POLL_INTERVAL_SECONDS` | `2` | How often the chat is read. |
| `OLLAMA_URL`, `OLLAMA_MODEL` | `http://localhost:11434`, `qwen2.5:7b-instruct` | Local model. |
| `LLM_TIMEOUT_SECONDS` | `20` | Per message. |
| `MAX_ATTEMPTS` | `5` | Tries for a message that keeps timing out, before it is given up and noted. |
| `RETRY_MAX_BACKOFF_SECONDS` | `60` | Longest wait between retries. |
| `MAX_DAYS_AHEAD` | `31` | Furthest order date accepted. |
| `INCLUDE_OWN_MESSAGES` | `false` | Process messages from the logged-in (shop) account. |
| `AUDIT_DB_PATH` | `./data/audit.db` | Audit log. |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail. |

Rarely changed: `SELECTORS_PATH`, `ZALO_URL`, `PW_SESSION`, `PW_PROFILE_DIR`, `STATE_DB_PATH` (default `./data/acked.db`), `BROWSER_MAX_RESTARTS`, `BACKFILL_MAX_STEPS`, `BACKFILL_SETTLE_SECONDS`, `PUBLISH_TIMEOUT_SECONDS`.

## 5. Runbook

Problems show in the status line and the log. **Nothing is lost while a problem lasts**: messages wait and are processed when it is fixed. Message text and the API key are never written to the log.

### Reader states

| State | Meaning | What to do |
|---|---|---|
| `READY` | Reading the group chat. | Nothing. |
| `LOGIN_REQUIRED` | Zalo Web is logged out. | Scan the QR code in the Chrome window with the shop's phone. |
| `CHAT_NOT_FOUND` | No group with exactly the `ZALO_CHAT` title was found, or two groups share that title. It keeps retrying every 30 s and never opens a different chat. | Check the title in Zalo (copy it exactly into `ZALO_CHAT`) and restart. If two groups have the same title, rename one in Zalo. |
| `NO_CHAT_OPEN` | No chat is open yet (it is opening the group), or `ZALO_CHAT` is empty and the open chat is not a group. | Wait a few seconds. With `ZALO_CHAT` empty, open the group chat in the window. |
| `SELECTOR_DRIFT` | Zalo changed its web page and messages can no longer be read. | Developer task: update `src/zalo_order_assistant/zalo_reader/selectors.json` (see section 7). |
| `BROWSER_FAILED` | The Chrome window could not be reached. It is relaunched up to `BROWSER_MAX_RESTARTS` times, then the assistant stops. | Close any stuck Chrome window of the assistant and run `./start.sh` again. |

### Pipeline states

| State | Meaning | What to do |
|---|---|---|
| `OK` | Orders are being saved. | Nothing. |
| `PUBLISHER_UNAUTHORIZED` | Supabase rejected the API key (401/403). Repeated in the log every 60 s. | Create a new key in the Supabase app, put it in `.env` as `SUPABASE_API_KEY`, then restart (Ctrl+C, `./start.sh`). Waiting messages are then saved, with no duplicates. |
| `PUBLISHER_UNAVAILABLE` | Supabase cannot be reached, or is overloaded (timeouts, 429, 5xx). | Check the internet connection and the Supabase status page. It retries by itself. |
| `EXTRACTOR_UNAVAILABLE` | Ollama is not running, or the model is not installed. | Start Ollama (`ollama serve` or the Ollama app). If the log says `model missing`, run `ollama pull qwen2.5:7b-instruct`. |

While a pipeline problem lasts, the queue waits on the oldest message, so orders keep their order.

## 6. Finding a message that made no order

Every message that was read but produced no order is in the audit log with its Zalo message key, the time it was written, the reason and a status. The message text is not stored. Find the message in Zalo by its time.

```bash
sqlite3 -header -column data/audit.db \
  "SELECT sent_at, status, reason, key FROM audit ORDER BY sent_at DESC LIMIT 30"
```

| Status | Meaning |
|---|---|
| `not_an_order` | Chat, a question, a receipt, a past day, several days, an invalid date, beyond the horizon, a change with no number (`bad quantity`), or a change the message does not say in words (`unclear change`) (see `reason`). |
| `not_applied` | A reduce, set or cancel with no open order for that customer and day (`no_open_order`): already delivered, never ordered, or the order is on another day. Check with the customer and fix it in the Supabase app. |
| `skipped` | From the shop's own account, or the sender's name could not be read. |
| `failed` | Supabase refused the order as invalid (`publisher 422`), the message key was already stored with other content (`publisher 409: idempotency_conflict`, after a crash and a different second answer; the first line stands), or the model answer was unusable. |
| `retry_cap` | The model timed out `MAX_ATTEMPTS` times on this message. |

To add a missed order, create it in the Supabase app by hand.

## 7. Do not wipe `data/`

`data/` holds the Zalo login (`browser-profile`), the list of messages already handled (`acked.db`) and the audit log (`audit.db`).

**Do not delete `data/` or `acked.db`.** If you do, every message of the last `BACKFILL_DAYS` days is read and sent again. Orders that staff **deleted** in the app would come back, and you would have to scan the QR code again. Back the folder up instead, if needed.

## 8. For developers

```bash
uv run pytest                 # unit tests (no network, no model)
uv run pytest -m llm          # fixture set against the local model
uv run pytest -m e2e          # end to end: replayed chat, real Qwen, a local Supabase stand-in
uv run pytest -m browser      # chat-search scripts in a headless browser
uv run ruff check . && uv run mypy
uv run python scripts/extractor_accuracy.py --label "my change"   # rewrite reports/extractor-accuracy.md
```

`E2E_SUPABASE_URL=... E2E_SUPABASE_API_KEY=... uv run pytest -m e2e` runs the end-to-end test against a real Supabase **test** project. Never point it at production.

Layout (spec §2.8): `coordinator.py` is the mediator, with all retry and ack rules. `zalo_reader/`, `order_extractor/` and `order_publisher/` are the three colleagues, and `main.py` wires them.

### Verifying the chat search (open check of T5b)

The search-box selectors were checked on live Zalo Web, read-only. The search-result list was not, so `search_result_item` in `selectors.json` is still a guess. To check it, with the shop account logged in:

1. Set `ZALO_CHAT` to the group title. Open a **different** chat in the window, and make sure the group is not in the visible conversation list (scroll the list, or use an old group). Start the assistant. The log should show `chat_search scope=search ... result=match`, then `reader_state state=READY`.
2. Put the title of a private chat in `ZALO_CHAT`. Expect `CHAT_NOT_FOUND`, and no chat is opened.
3. Put a title that does not exist. Expect `CHAT_NOT_FOUND`.

If step 1 shows `scope=search candidates=0`, open the search results in Chrome DevTools, find the element of one result, and set `search_result_item` to a selector matching each result (the result must contain the `.conv-item-title__name .truncate` title and, for groups, an `anim-data-id` starting with `g`).

### Checking the publisher against the real app (open check of T3)

`uv run python scripts/check_publisher.py` sends add 3, reduce 1, set 4 and cancel for a throw-away customer today, replays the first line, and sends a reduce with nothing open. It prints the total after each step (3, 2, 4, 0, then 200, then not applied). Delete the cancelled test order in the app afterwards.
