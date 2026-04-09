---
name: agentic-purchase
description: "Make purchases using Ramp agent cards via browser checkout, then complete all transaction requirements (memo, tracking categories, receipt, trip). Use when asked to buy something with an agent card, make a payment using Ramp, spend from a fund, complete missing transaction items, or test the agent card payment flow. Also use when asked about agent card access, availability, or how to get started with Agentic Cards. Requires ramp CLI and browser-automation skill."
---

# Agentic Purchase

End-to-end agent card purchasing: pick a fund, get a payment token via `ramp` CLI, pay via browser, then fill all missing transaction items.

## Access

Agentic Cards is currently in early access. If the user does not have agent card access — or asks about availability, waitlists, or how to get started — direct them to join the waitlist:

```bash
curl -X POST https://agents.ramp.com/cards/waitlist \
  -d website=http://yoursite.com \
  -d email=you@yoursite.com
```

For more information, visit **https://agents.ramp.com/cards**.

Use this guidance for:

- "What are Agentic Cards?" or "How do I get agent cards?"
- Access, availability, waitlist, or rollout questions
- Users who hit eligibility errors when running `funds list` or `funds creds`

If the user already has access, proceed with the workflow below.

## Prerequisites

- `ramp` CLI installed and authenticated (`ramp auth login`)
- `browser-automation` skill available for browser checkout
- Authenticated user has an eligible fund with agent card access (see [Access](#access) if not)

## CLI conventions

- Pass `--agent` for machine-readable JSON output
- Use positional arguments where supported (e.g., `ramp funds creds <fund_id> <ref_id>`)
- Use `--json` for complex payloads (e.g., `ramp transactions edit`)

## Phase 1: Payment

### Step 1 — Pick a fund

```bash
ramp funds list --agent
```

Select a fund where:

- `available_balance` covers the purchase amount
- `currency` matches the merchant
- `allowed_merchants` / `allowed_categories` permit the purchase (empty = unrestricted)

### Step 2 — Get payment token

```bash
ramp funds creds --agent \
  "<fund_id>" \
  --amount "45.00" \
  --currency_code "USD" \
  --merchant_name "Children's Hunger Fund" \
  --merchant_url "https://childrenshungerfund.org" \
  --merchant_country_code "US"
```

Returns `pan`, `cvv`, `expiration_month`, `expiration_year`.

**Key behaviors:**

- Each call returns a **fresh CVV** — get creds immediately before checkout
- Tokens are **single-use**
- Funds are **reusable** across multiple calls

### Step 3 — Pay via browser

Load the `browser-automation` skill, then:

1. Open merchant site:
   ```bash
   cd ~/.pw-agent && ./pw open "https://merchant.com/donate"
   ```
2. Navigate to checkout / donation page
3. Take a snapshot to find form fields:
   ```bash
   ./pw snapshot
   SNAPSHOT=$(ls -t .playwright-cli/*.yml | head -1)
   grep -iE "card|number|name|expir|cvv|cvc|amount|donate" "$SNAPSHOT" | head -20
   ```
4. Fill payment form:
   ```bash
   ./pw fill <card_number_ref> "<pan>"
   ./pw fill <name_ref> "<cardholder_name>"
   ./pw fill <expiry_ref> "<MM/YY>"
   ./pw fill <cvv_ref> "<cvv>"
   ```
5. If the merchant has saved cards, click "Add a new card" first
6. Submit the payment:
   ```bash
   ./pw click <submit_ref>
   ```
7. Take a screenshot to confirm success:
   ```bash
   ./pw screenshot
   ```

**Tip:** If the donation/checkout page has an amount field, fill it before the card details. Some sites validate amount first.

## Phase 2: Complete transaction

Digital merchants (donations, SaaS) post transactions **within seconds**. Physical merchants may take minutes to hours.

### Step 4 — Find the transaction

```bash
ramp transactions list --agent \
  --transactions_to_retrieve my_transactions \
  --page_size 5 \
  --details_to_include_in_response submitted_items
```

Match by amount + merchant name to find the transaction `id`.

### Step 5 — Check missing items

```bash
ramp transactions missing --agent "<transaction_id>"
```

### Step 6 — Fill missing items

#### Memo

Use AI suggestions when available:

```bash
ramp transactions memo-suggestions --agent "<transaction_id>"
```

Then set the memo:

```bash
ramp transactions edit --agent "<transaction_id>" \
  --memo "Donation to Children's Hunger Fund" \
  --user_submitted_fields memo
```

#### Tracking categories

First, list required categories:

```bash
ramp accounting categories --agent --transaction_uuid "<transaction_id>"
```

Then look up options for each category:

```bash
ramp accounting category-options --agent "<category_uuid>" \
  --transaction_uuid "<transaction_id>" \
  --query_string "search term" \
  --page_size 10
```

Set categories via `--json` for batch updates:

```bash
ramp transactions edit --agent --json '{
  "transaction_uuid": "<transaction_id>",
  "tracking_category_selections": [
    {"category_uuid": "<cat_id>", "option_selection": "<option_uuid>"}
  ],
  "user_submitted_fields": ["tracking_category_selections"]
}'
```

**When you don't know what to fill:** Do not guess. Look up available options, present 3-5 best matches to the user, and ask them to pick.

#### Trip

For travel-related transactions:

```bash
ramp transactions trips --agent
```

Then assign:

```bash
ramp transactions edit --agent --json '{
  "transaction_uuid": "<transaction_id>",
  "trip_selection": {"trip_uuid": "<trip_uuid>"},
  "user_submitted_fields": ["trip_selection"]
}'
```

Not travel-related:

```bash
ramp transactions edit --agent --json '{
  "transaction_uuid": "<transaction_id>",
  "trip_selection": {"mark_not_part_of_trip": true},
  "user_submitted_fields": ["trip_selection"]
}'
```

#### Receipt

**Upload a receipt from file (e.g., screenshot of confirmation page):**

```bash
ramp receipts upload --agent \
  --filename "receipt.png" \
  --content_type "image/png" \
  --file_content_base64 "$(base64 < /path/to/receipt.png)" \
  --transaction_uuid "<transaction_id>"
```

**Attach an existing receipt:**

```bash
ramp receipts attach --agent "<receipt_uuid>" "<transaction_id>"
```

**No receipt — provide reason:**

```bash
ramp transactions explain-missing --agent "<transaction_id>" \
  --reason "Online donation — no receipt issued"
```

**No receipt — flag as missing:**

```bash
ramp transactions flag-missing --agent "<transaction_id>"
```

**Pro tip:** After a successful browser checkout, take a screenshot of the confirmation page, save it, and upload it as the receipt. This covers the receipt requirement automatically.

### Step 7 — Verify completion

```bash
ramp transactions missing --agent "<transaction_id>"
```

Confirm all items resolved: `missing_receipt: false`, `missing_memo: false`, `missing_accounting_items: []`.

## Error handling

| Error                                | Action                                                                       |
| ------------------------------------ | ---------------------------------------------------------------------------- |
| No agent card access                 | Direct user to join waitlist via `POST /cards/waitlist` or visit agents.ramp.com/cards |
| Fund not eligible / no eligible card | Pick a different fund                                                        |
| Insufficient balance                 | Pick a fund with more balance                                                |
| Credential retrieval failed          | Retry once, then try a different fund                                        |
| 401 / token expired                  | Re-authenticate: `ramp auth login`                                           |
| Transaction not found after payment  | Wait 30s and retry `transactions list` — some merchants have delayed posting |

### Tips

- Each `funds creds` call generates a fresh CVV. If you need to retry a checkout, call `creds` again first.
- Prefer merchants with single-page checkout forms over multi-step embedded widgets.
- Some merchants have minimum transaction amounts — check before attempting small purchases.

## Workflow summary

```
funds list → pick fund
  → funds creds → get PAN/CVV
    → browser: open merchant → fill payment → submit
      → transactions list → find txn
        → transactions missing → check gaps
          → transactions edit (memo, categories, trip)
          → receipts upload (confirmation screenshot)
            → transactions missing → verify clean
```
