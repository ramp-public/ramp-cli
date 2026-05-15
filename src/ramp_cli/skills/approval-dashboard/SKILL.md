---
name: approval-dashboard
description: |-
  Review and approve pending transactions, bills, reimbursements, and requests.
  Use when: 'approve', 'pending approvals', 'what needs my approval',
  'review transactions', 'approve bills', 'reject', 'approval queue',
  'clear my approvals'. Do NOT use for: transaction analysis, receipt uploads,
  or spend tracking.
---

## Non-Negotiables

- Always show the item details before approving or rejecting. Never blind-approve.
- Rejections require a reason. Approvals do not, but a note is helpful.
- Confirm with the user before executing approvals — especially bulk operations.
- Present items sorted by priority: highest dollar amount first.
- Amounts vary by endpoint: bills are in **cents** (divide by 100), reimbursements are in **dollars**, transactions are formatted strings ("$135.40").
- **Deep links**: If the response contains a `bill_url` (bills) or `reimbursement_link` (reimbursements) field, include it when presenting entity details so the user can click through to the Ramp web app. If these fields are absent, direct the user to the relevant Ramp page (e.g., `https://app.ramp.com/bills`) instead. Never fabricate deep link URLs.

## Workflow

### Step 1: Fetch all pending items

Run these in parallel to build the full approval queue. **Paginate each endpoint until there are no more results** — a single page may not return everything.

```bash
# Pending transactions (paginate with --next_page_cursor)
ramp transactions list --transactions_to_retrieve transactions_awaiting_my_approval \
  --agent --page_size 50

# Pending bills (paginate with --page_cursor)
ramp bills pending --agent --limit 50

# Pending reimbursements (no cursor pagination — use --limit)
ramp reimbursements pending --agent --limit 50

# Pending requests (paginate with --start; --thoughts is required)
ramp requests pending --thoughts "Reviewing all pending requests" --page_size 50 --agent
```

For each endpoint, check `pagination.next_cursor` in the JSON envelope. If it is not null, re-run the command with that cursor value (via `--next_page_cursor` for transactions, `--page_cursor` for bills, `--start` for requests) until all pages are fetched. Note: `reimbursements pending` does not support cursor-based pagination — it only has `--limit`, so increase the limit if you need more results. Aggregate results before presenting.

### Step 2: Present the queue

Summarize what's pending. If the response includes `bill_url` or `reimbursement_link` fields, include them so the user can open items directly in the Ramp web app:

```
Approval queue: 14 items ($23,450 total)

Bills (4 items, $8,200):
  $3,500  HighSpot        Invoice #1234    Due 2026-03-28  → <bill_url if present>
  $2,500  Cometeer        Invoice #5678    Due 2026-04-01  → <bill_url if present>
  ...

Reimbursements (6 items, $2,100):
  $  520  Michael Scott   Uber rides       Submitted 2026-03-20  → <reimbursement_link if present>
  $  312  Oscar Martinez  Office supplies  Submitted 2026-03-22  → <reimbursement_link if present>
  ...

Transactions (3 items, $12,500):
  $5,000  Dana Alhasawi   AWS              2026-03-15
  ...

Requests (1 item, $650):
  ...
```

### Step 3: Review and act

For each item the user wants to act on, get details first:

```bash
# Bill details
ramp bills get {bill_id} --agent

# Transaction details
ramp transactions get {transaction_uuid} --agent

# Transaction missing items (if relevant)
ramp transactions missing {transaction_uuid}

# Reimbursement details (use list with specific UUID)
ramp reimbursements list --reimbursement_uuids '["{uuid}"]' --include_policy_assessment --agent
```

### Step 4: Execute approvals

```bash
# Approve a transaction
ramp transactions approve {transaction_uuid} --action APPROVE --thoughts "Reviewed — within policy"

# Reject a transaction (reason required)
ramp transactions approve {transaction_uuid} \
  --action REJECT_AND_REQUEST_CHANGES \
  --thoughts "Missing receipt and over budget" \
  --user_reason "Please attach the receipt and update the memo"

# Approve a bill — not yet available via CLI.
# Use the bill_url from the response to direct the user to the Ramp app.

# Approve a reimbursement
ramp reimbursements approve {reimbursement_uuid} --action approve

# Reject a reimbursement
ramp reimbursements approve {reimbursement_uuid} --action reject \
  --user_reason "Receipt doesn't match the claimed amount"

# Approve a request
ramp requests approve {request_uuid} --action APPROVE --thoughts "Approved — within team budget"
```

### Step 5: Add comments (optional)

Leave a comment on any Ramp object for context:

```bash
ramp general comment {object_uuid} \
  --ramp_object_type transaction \
  --message "Approved — this covers the Q1 offsite catering"
```

Valid object types: `bill`, `booking_request`, `card`, `payee`, `provisional_bill`, `reimbursement`, `spend_intent`, `spend_request`, `ticket`, `transaction`, `vendor_payment`.

## Bulk Approval

When the user wants to approve multiple items at once:

1. Present all items with amounts and details
2. Ask: "Approve all N items, or would you like to review individually?"
3. If bulk: confirm once, then execute sequentially
4. Report results: "Approved 12/14 items. 2 failed: [reasons]"

## Transaction Approval Actions

| Action | When to use |
|---|---|
| `APPROVE` | Item is within policy, receipt/memo present |
| `REJECT_AND_REQUEST_CHANGES` | Needs corrections — missing memo, wrong coding, etc. |
| `REJECT_AND_REQUEST_FULL_REPAYMENT` | Personal expense or policy violation requiring repayment |

## Example Session

```
User: What needs my approval?

Agent: Let me check all your pending queues.
> ramp transactions list --transactions_to_retrieve transactions_awaiting_my_approval --agent --page_size 50
> ramp bills pending --agent --limit 50
> ramp reimbursements pending --agent --limit 50
> ramp requests pending --thoughts "Checking approval queue" --page_size 50 --agent

Approval queue: 6 items ($4,350 total)

Bills (2 items, $2,700):
  $2,200  HighSpot        Invoice #4401    Due 2026-03-28
  $  500  Cometeer        Invoice #882     Due 2026-04-01

Reimbursements (3 items, $1,430):
  $  520  Michael Scott   Uber rides       Submitted 2026-03-20
  $  312  Oscar Martinez  Office supplies  Submitted 2026-03-22
  $  598  Jan Levinson    Flight change    Submitted 2026-03-23

Transactions (1 item, $220):
  $  220  Dana Alhasawi   DoorDash         2026-03-15

Want to review individually or approve all?

User: Approve the bills and reimbursements. Let me see the transaction first.

Agent: I can approve the reimbursements now. Bills must be approved in the Ramp app.
> ramp reimbursements approve reimb-001 --action approve
> ramp reimbursements approve reimb-002 --action approve
> ramp reimbursements approve reimb-003 --action approve

3 reimbursements approved. For the 2 bills, approve them in the Ramp app:
  HighSpot Invoice #4401 → <bill_url if present, else https://app.ramp.com/bills>
  Cometeer Invoice #882  → <bill_url if present, else https://app.ramp.com/bills>

Now let me get details on that DoorDash transaction:
> ramp transactions get txn-001 --agent
> ramp transactions missing txn-001

DoorDash $220 — Dana Alhasawi, 2026-03-15
Memo: "Team lunch"  |  Receipt: attached  |  Accounting: complete

User: Approve it

Agent: > ramp transactions approve txn-001 --action APPROVE --thoughts "Team lunch — receipt and memo present"
Done. Approval queue is clear.
```

## Deep Link Handoff

When the agent hits a limitation — bill approvals aren't supported via CLI, or step-up auth is required — hand off to the user with a link to the Ramp web app:

- **Bills**: If the response includes a `bill_url` field, use it — it routes to the correct bill page based on status (draft, approval, or paid). If absent, direct the user to `https://app.ramp.com/bills`.
- **Reimbursements**: If the response includes a `reimbursement_link` field, use it. If absent, direct the user to the Ramp reimbursements page.

Example handoff message (when `bill_url` is present):
```
I can't approve bills via the CLI. You can approve this bill directly in Ramp:
  $3,500 HighSpot Invoice #1234 → <bill_url>
```

Always prefer deep link fields from the API response when available — they account for bill status and environment. Never fabricate deep link URLs.

## When NOT to Use

- **Uploading receipts** — use receipt-compliance
- **Editing transaction memos or categories** — use transaction-cleanup

## Gotchas

| Issue | Fix |
|---|---|
| Bill amounts are in cents | Divide by 100 for display |
| Reimbursement amounts are in dollars | Display as-is |
| Transaction amounts are formatted strings | Strip "$" and "," for sorting/totaling |
| `requests pending` requires `--thoughts` | Always include it — describe what you're doing |
| Bill approvals are not yet available via CLI | Send the user to the Ramp app instead: `https://app.ramp.com/bills` |
| No undo for approvals | Confirm with user before executing. Use `-n` for dry runs on write commands. |
| Pagination varies | Check `pagination.next_cursor` in envelope. Pass it via `--next_page_cursor` (transactions), `--page_cursor` (bills), `--start` (requests). Reimbursements: `--limit` only. |
