---
name: browserbase
description: |-
  Cloud browser automation for Ramp workflows — fetch receipts from travel and
  vendor portals, and complete checkout flows on bot-protected merchant sites.
  Use when: 'fetch my receipt', 'pull travel receipt', 'get hotel folio',
  or when the browser-automation skill fails due to bot detection or CAPTCHAs.
  Drop-in replacement for browser-automation in the agentic-purchase workflow.
  Requires bb CLI and browse CLI. Remote mode requires a Browserbase account.
dependencies: [browser]
---

# Browserbase — Cloud Browser for Ramp

Two modes — use whichever fits your setup:

| | **Local mode** | **Remote mode (Browserbase)** |
|---|---|---|
| Requires | Local Chrome | Browserbase account + `BROWSERBASE_API_KEY` |
| Stealth / anti-bot | No | Yes (Enterprise only) |
| Residential proxies | No | Yes (Enterprise only) |
| CAPTCHA solving | No | Yes |
| Session recordings | No | Yes |
| Best for | Development, simple sites | Protected portals, CI/headless |

Start with local. Escalate to remote only if the site blocks you.

## Non-Negotiables

- **Never upload a receipt without confirming the match** — wrong receipt on wrong transaction is worse than no receipt. Use `-n` (dry run) before real uploads.
- **Always verify after upload** — run `ramp transactions missing {uuid}` to confirm `missing_receipt: false`.
- **Ask before remote mode** — on macOS, confirm with the user before switching to remote. They may want to watch the local browser.
- **Never guess credentials** — if a portal requires login, ask the user. Never invent passwords.
- **All `ramp` CLI flags use underscores**, not hyphens (e.g., `--transaction_uuid`, `--content_type`).
- **Pass `--agent`** on all `ramp` commands for machine-readable JSON output.

## Prerequisites

```bash
# Install CLIs
npm install -g @browserbasehq/cli @browserbasehq/browse-cli

# Install the Browserbase browser skill (teaches Claude the full browse CLI)
bb skills install
```

**For remote mode only:**

```bash
export BROWSERBASE_API_KEY="your_api_key"       # from browserbase.com/settings
export BROWSERBASE_PROJECT_ID="your_project_id"
```

---

## Switching Modes

```bash
browse env           # show current mode
browse env local     # use local Chrome (default)
browse env remote    # use Browserbase cloud (requires account)
```

---

## Core Commands

Same commands work in both modes:

```bash
browse open <url>               # navigate to URL
browse snapshot                 # accessibility tree with element refs
browse screenshot [path]        # visual screenshot
browse click <ref>              # click element by ref (e.g. @0-5)
browse fill <selector> <value>  # fill input field
browse press <key>              # press key (Enter, Tab, Escape, etc.)
browse get text body            # get all page text
browse stop                     # close session
```

**Typical pattern:**
1. `browse open <url>` — navigate
2. `browse snapshot` — read tree, find element refs
3. `browse click <ref>` / `browse fill <selector> <value>` — interact
4. `browse snapshot` — confirm, get fresh refs
5. Repeat until done
6. `browse screenshot /tmp/result.png` — capture for upload
7. `browse stop`

---

## Workflow A — Fetch a Receipt

### Step 1 — Navigate and capture

**Local mode (try first):**

```bash
browse open "https://airline.com/mytrips"
browse snapshot
browse click <trips-list-ref>       # select the trip
browse snapshot
browse click <receipt-link-ref>     # open receipt / e-ticket
browse snapshot
browse screenshot /tmp/receipt.png
browse stop
```

**Remote mode (if local is blocked):**

```bash
# Basic — CAPTCHA solving
SESSION_ID=$(bb sessions create --solve-captchas | jq -r '.id')
browse --connect $SESSION_ID open "https://airline.com/mytrips"
# ... same browse commands ...
browse stop

# Full — stealth + proxies + CAPTCHA (Enterprise only)
SESSION_ID=$(bb sessions create --advanced-stealth --proxies --solve-captchas | jq -r '.id')
browse --connect $SESSION_ID open "https://airline.com/mytrips"
# ... same browse commands ...
browse stop
```

### Step 2 — Dry run upload first

Always confirm the match before uploading:

```bash
ramp receipts upload \
  --content_type "image/png" \
  --filename "receipt.png" \
  --file_content_base64 "$(base64 -i /tmp/receipt.png | tr -d '\n')" \
  --transaction_uuid "<transaction_id>" \
  --agent \
  -n
```

Review the dry-run output. If it looks correct, upload for real (drop `-n`):

```bash
ramp receipts upload \
  --content_type "image/png" \
  --filename "receipt.png" \
  --file_content_base64 "$(base64 -i /tmp/receipt.png | tr -d '\n')" \
  --transaction_uuid "<transaction_id>" \
  --agent
```

Also supported: `application/pdf`, `image/jpeg`, `image/heic`, `image/webp`.

### Step 3 — Verify

```bash
ramp transactions missing "<transaction_id>" --agent
```

Confirm `missing_receipt: false` before closing out.

---

## Workflow B — Checkout (drop-in for browser-automation)

Use this in place of the `browser-automation` skill within the `agentic-purchase` workflow when the merchant site blocks local Playwright.

**Local mode (default):**

```bash
browse open "https://merchant.com/checkout"
browse snapshot
browse fill <card-number-ref> "<pan>"
browse fill <expiry-ref> "<MM/YY>"
browse fill <cvv-ref> "<cvv>"
browse click <submit-ref>
browse screenshot /tmp/confirmation.png
browse stop
```

**Remote mode (bot-protected merchant):**

```bash
SESSION_ID=$(bb sessions create --advanced-stealth --proxies --solve-captchas | jq -r '.id')
browse --connect $SESSION_ID open "https://merchant.com/checkout"
# ... same browse commands ...
browse stop
```

Then continue with the `agentic-purchase` Phase 2 (find transaction, fill missing items).

---

## Common Receipt Patterns

### Airline (trips page — requires login)
```
browse open "https://airline.com/mytrips"
→ select trip → Receipt / E-ticket → screenshot
```

### Airline (booking ref — no login needed)
```
browse open "https://airline.com/reservation/retrieve"
→ enter booking ref + last name → Receipt → screenshot
```

### Hotel (loyalty account)
```
browse open "https://hotel.com/account/activity"
→ select stay → View Folio / Receipt → screenshot
```

### Ride-share
```
browse open "https://ride-share.com/trips"
→ select trip → Download Receipt → screenshot
```

### SaaS / subscription
```
browse open "https://app.example.com/billing"
→ find invoice → Download PDF → screenshot
```

---

## Session Recordings (remote mode only)

Every remote session is recorded for audit purposes:

```bash
bb sessions list                        # find recent session IDs
bb sessions recording <session_id>      # get recording URL
```

Watch live at `browserbase.com/sessions` during an active session.

---

## When NOT to Use

- **You already have the receipt file locally** — use `receipt-compliance` directly, no browser needed
- **Simple sites that work fine with local Playwright** — use `browser-automation` (faster)
- **Memo or category cleanup** — use `transaction-cleanup`
- **Approvals** — use `approval-dashboard`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Site blocks local browser | Use `bb sessions create --solve-captchas` + `browse --connect` |
| Still blocked after remote | Add `--advanced-stealth --proxies` (Enterprise required) |
| `BROWSERBASE_API_KEY not set` | `export BROWSERBASE_API_KEY="..."` (remote mode only) |
| `browse` not found | `npm install -g @browserbasehq/browse-cli` |
| `jq` not found | `brew install jq` |
| Snapshot shows wrong page | `browse get url` to confirm current URL |
| Upload says receipt already exists | Check `receipt_uuids` on the transaction first — skip if populated |

```bash
browse env           # confirm current mode
bb projects list     # verify Browserbase API key (remote mode only)
```

## Gotchas

| Issue | Fix |
|---|---|
| `--advanced-stealth` and `--proxies` not working | Requires Browserbase Enterprise plan |
| `base64` flag differs by OS | macOS: `base64 -i <file>`. Linux: `base64 <file>`. Both need `\| tr -d '\n'` |
| Refs go stale after page changes | Always re-`snapshot` after navigation or AJAX updates |
| `bb sessions create` output format | Pipe through `jq -r '.id'` to extract the session ID |
| `-n` (dry run) not available on all commands | Only write commands support it — `receipts upload` does |
