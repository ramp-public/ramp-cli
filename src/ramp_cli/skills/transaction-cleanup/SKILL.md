---
name: transaction-cleanup
description: |-
  Complete missing items on your transactions — memos, accounting categories, funds,
  and attendees. Use when: 'add memo', 'categorize transactions', 'missing items',
  'transaction cleanup', 'fix my transactions', 'set tracking category',
  'assign to fund', 'bulk memo update'. Do NOT use for: receipt uploads
  (use receipt-compliance) or approvals (use approval-dashboard).
---

## Non-Negotiables

- Scope to the user's own transactions by default (`--transactions_to_retrieve my_transactions`).
- Show the transaction details before editing. Never blind-edit.
- For bulk edits, present the plan and confirm before executing.
- The `--user_submitted_fields` flag tracks provenance — include it to mark which fields the user explicitly provided vs agent-inferred.
- All CLI flags use **underscores**, not hyphens.

## Workflow

### Step 1: Find transactions needing attention

```bash
# Transactions awaiting your action (usually means missing items)
ramp transactions list --transactions_to_retrieve my_transactions \
  --from_date {start} --state cleared --agent --page_size 50
```

For any transaction, check what's missing:
```bash
ramp transactions missing {transaction_uuid}
```

Returns: `missing_receipt` (bool), `missing_memo` (bool), `missing_accounting_items` (array of category names).

### Step 2: Get AI-suggested memos

Before writing memos manually, check if Ramp has suggestions:
```bash
ramp transactions memo-suggestions {transaction_uuid}
```

Returns `memos[]` — an array of suggested memo strings based on the transaction context.

### Step 3: Edit transactions

#### Set a memo
```bash
ramp transactions edit {transaction_uuid} --memo "Q2 team offsite catering"
```

To clear a memo, pass an empty string: `--memo ""`

#### Assign to a fund/spend allocation
```bash
ramp transactions edit {transaction_uuid} --fund_uuid {fund_uuid}
```

To find available funds:
```bash
ramp funds list --funds_to_retrieve MY_FUNDS --include_balance --agent
```

#### Set tracking categories (accounting codes)

First, get available categories and their options:
```bash
# List categories
ramp accounting categories --agent

# List options for a specific category (use UUID from above)
ramp accounting category-options {tracking_category_uuid} --agent --page_size 50
```

Then edit via `--json` (tracking categories aren't exposed as named flags).

**Important:** When using `--json`, you must include `transaction_uuid` in the body. The `--json` flag bypasses the CLI's automatic injection of the positional arg.

```bash
ramp transactions edit --json '{
  "transaction_uuid": "{transaction_uuid}",
  "tracking_category_selections": [
    {
      "category_uuid": "{category_uuid}",
      "option_selection": "{option_uuid}"
    }
  ],
  "user_submitted_fields": ["tracking_category_selections"]
}'
```

Note: the field names inside `tracking_category_selections` are `category_uuid` and `option_selection` — NOT `tracking_category_uuid` / `tracking_category_option_uuid` (those are the names returned by the categories list endpoint, not the edit endpoint).

#### Set attendees
```bash
ramp transactions edit --json '{
  "transaction_uuid": "{transaction_uuid}",
  "attendee_selections": {
    "non_ramp_attendees": [
      {"attendee_name": "Jane Smith", "attendee_email": "jane@company.com"}
    ],
    "include_self_as_attendee": false
  },
  "user_submitted_fields": ["attendee_selections"]
}'
```

### Step 4: Handle missing receipts (hand off)

If `missing_receipt` is true, you have two options:

1. **Upload a receipt** — use the receipt-compliance skill
2. **Explain why it's missing**:
```bash
ramp transactions explain-missing {transaction_uuid} \
  --reason "Digital subscription — no receipt issued"
```

Or generate a link to the missing receipt affidavit form (the user must complete it manually in the browser):
```bash
ramp transactions flag-missing {transaction_uuid}
```

## Bulk Cleanup Workflow

For cleaning up many transactions at once:

1. Fetch all transactions in the period
2. Check `missing_items` on each (or batch-check via individual calls)
3. Group by what's missing: memo, accounting, receipt
4. Present summary: "12 transactions need memos, 5 need accounting categories, 3 need receipts"
5. For memos: fetch suggestions for each, present them, let user confirm or edit
6. For categories: if most transactions share a category, offer to bulk-apply
7. Execute edits sequentially, report progress

## Provenance Tracking

The `--user_submitted_fields` flag tells Ramp which fields the user explicitly provided. This matters for compliance — it distinguishes user intent from agent inference.

Include it when setting fields via `--json`:
```bash
ramp transactions edit --json '{
  "transaction_uuid": "{transaction_uuid}",
  "memo": "Client dinner with Acme team",
  "user_submitted_fields": ["memo"]
}'
```

Valid values: `attendee_selections`, `attendee_uuids`, `fund_uuid`, `memo`, `tracking_category_selections`.

When using named flags (`--memo`, `--fund_uuid`), provenance is tracked automatically.

## Example Session

```
User: I have a bunch of transactions that need memos

Agent: Let me check what's missing on your recent transactions.
> ramp transactions list --transactions_to_retrieve my_transactions \
>   --from_date 2026-03-01 --state cleared --agent --page_size 50

Found 8 transactions. Checking missing items on each...
> ramp transactions missing {uuid} (×8)

Summary:
  5 need memos
  2 need accounting categories
  1 needs a receipt (→ use receipt-compliance)

Let me get AI-suggested memos for the 5:
> ramp transactions memo-suggestions {uuid} (×5)

Transaction                              Suggested memo
$135  Facebook Ads     2026-03-02       "Q1 paid social campaign"
$220  AWS              2026-03-05       "March infrastructure costs"
$89   Uber             2026-03-10       (no suggestion)
$450  WeWork           2026-03-12       "March coworking space"
$65   Doordash         2026-03-15       (no suggestion)

Want to accept these suggestions, edit them, or write your own?

User: Accept the suggestions for Facebook and AWS. The Uber was a client dinner ride, WeWork is fine, skip DoorDash for now.

Agent: Updating 4 transactions:
> ramp transactions edit {fb-uuid} --memo "Q1 paid social campaign"
> ramp transactions edit {aws-uuid} --memo "March infrastructure costs"
> ramp transactions edit {uber-uuid} --memo "Client dinner transportation"
> ramp transactions edit {wework-uuid} --memo "March coworking space"

All 4 memos set. 2 transactions still need accounting categories.
Want to tackle those?

User: Yes, they should both be under the "Marketing" project

Agent: > ramp accounting categories --agent
> ramp accounting category-options {project-uuid} --agent --page_size 50

Found "Marketing" option (uuid: mkt-123). Applying to both:
> ramp transactions edit --json '{"transaction_uuid": "{uuid1}", "tracking_category_selections": [{"category_uuid": "proj-uuid", "option_selection": "mkt-123"}]}'
> ramp transactions edit --json '{"transaction_uuid": "{uuid2}", "tracking_category_selections": [{"category_uuid": "proj-uuid", "option_selection": "mkt-123"}]}'

Done. 1 transaction remaining (needs receipt — use receipt-compliance when ready).
```

## When NOT to Use

- **Uploading receipts** — use receipt-compliance (handles base64 encoding, matching, bulk upload)
- **Approving or rejecting items** — use approval-dashboard
- **Company-wide spend reporting** — the CLI is scoped to your own data; use the Ramp dashboard

## Gotchas

| Issue | Fix |
|---|---|
| `amount` is a formatted string ("$135.40") | Strip "$" and "," for numeric operations |
| `--state` values are lowercase | Use `cleared`, `pending` — not uppercase |
| Tracking categories require `--json` | Named flags only cover `--memo` and `--fund_uuid`. When using `--json`, include `transaction_uuid` in the body. |
| Category field names differ between endpoints | `accounting category-options` returns `tracking_category_option_uuid`, but `transactions edit` expects `category_uuid` + `option_selection` |
| `memo-suggestions` may return empty | Not all transactions have enough context for suggestions |
| `accounting category-options` paginates with integers | Unlike other endpoints, the cursor is a number, not a string |
| `--transactions_to_retrieve` is required | Always include it on `transactions list` |
| Searching for specific transactions | Use `--reason_memo_merchant_or_user_name_text_search "query"` (min 3 chars) |
| Comment on a transaction | `ramp general comment {uuid} --ramp_object_type transaction --message "text"` |
