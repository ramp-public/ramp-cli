---
name: payment-lookup
description: |-
  Look up vendor bill payments and verify payment status from the terminal.
  Use when: 'did we pay', 'payment status', 'check if paid', 'verify payment',
  'find invoice', 'bill lookup', 'was this bill paid', 'payment confirmation'.
  Do NOT use for: approving bills (use approval-dashboard), spend analysis
  across vendors (use spend-analysis), or uploading receipts (use receipt-compliance).
---

## Non-Negotiables

- Always search bills first, then enrich with `bills get` and `bills attachments` when available.
- Report exactly what the API returns. Don't infer payment state beyond what the status says.
- When checking whether a payment went out, search with `--include_paid` to include completed bills.
- Pass `--agent` for machine-readable JSON output on all commands.
- Bill amounts in search results are numeric dollars. Bill amounts in `get` responses may be in **cents** (divide by 100 if values look 100x too large). Transaction amounts are formatted strings (`"$135.40"`). Reimbursement amounts are in dollars.
- If `bills get` fails on a search-returned ID, fall back to the search results and note the limitation.

## Workflow

### Step 1: Search for the bill

```bash
ramp bills search --query "<vendor or invoice number>" --include_paid --limit 10 --agent
```

The `--query` flag matches against payee name, invoice number, and payment ID. Partial matches work (e.g., "UPS" matches "UPS Store").

If too many results, narrow with more specific terms. If zero results, try alternate vendor name spellings.

If `next_page_cursor` is not null, paginate with `--page_cursor`:

```bash
ramp bills search --query "<vendor>" --include_paid --limit 10 \
  --page_cursor "<cursor>" --agent
```

### Step 2: Drill into details

```bash
ramp bills get <bill_id> --agent
```

This returns full payment detail including status, approval history, and metadata.

Use `-n` (dry run) to preview the request without sending:

```bash
ramp bills get <bill_id> --agent -n
```

### Step 3: Pull the invoice attachment

```bash
ramp bills attachments <bill_id> --agent
```

Returns invoice files associated with the bill for cross-reference.

If `get` fails on a bill ID, `attachments` will likely fail too.

### Step 4: Present findings

Summarize clearly:

```
Bill found: DHL
  Invoice #: 24-165-12
  Amount: $1,287.95 USD
  Payment status: [status from API]
  Approval status: [status from API]
  Memo: Business package shipment and insurance
```

If the user wanted to confirm a payment went out, state explicitly whether the status confirms payment or not.

## Fields Available

### From `bills search`

| Field | Description |
|---|---|
| `id` | Bill UUID |
| `invoice_number` | Vendor invoice number |
| `vendor_name` | Payee name |
| `amount` | Numeric amount (dollars) |
| `currency` | Currency code |
| `due_date` | Payment due date (may be null) |
| `payment_status` | Payment state (may show "Unknown" for unpaid) |
| `approval_status` | Approval state |
| `memo` | Bill description |

### From `bills get` (when available)

Additional fields beyond search — exact schema depends on bill state. May include payment method, approval chain, and accounting metadata.

## Example Session

```
User: Did we pay DHL this month?

Agent: Let me check.
> ramp bills search --query "DHL" --include_paid --limit 10 --agent

Found 1 bill:
  DHL — Invoice #24-165-12
  Amount: $1,287.95 USD
  Payment status: PAID
  Approval status: APPROVED
  Memo: Business package shipment and insurance

Let me get full details.
> ramp bills get fe8e01ce-12a4-4512-bd27-da8a7432466d --agent

Bill found: DHL
  Invoice #: 24-165-12
  Amount: $1,287.95 USD
  Payment status: PAID
  Payment method: ACH
  Payment date: Mon, 14 Apr 2026
  Approval status: APPROVED

Yes, DHL was paid $1,287.95 via ACH on April 14.

User: What about PwC?

Agent: > ramp bills search --query "PwC" --include_paid --limit 10 --agent

Found 1 bill:
  PwC — Invoice #231458926-12
  Amount: $15,000.00 USD
  Payment status: OPEN
  Approval status: PENDING
  Memo: Consulting Hours

This bill hasn't been paid yet — it's still pending approval.
Want me to pull the invoice attachment?
```

## When NOT to Use

- **Approving or rejecting bills** — use `approval-dashboard`
- **Analyzing spend across multiple vendors** — use `spend-analysis`
- **Uploading receipts** — use `receipt-compliance`

## Gotchas

| Issue | Fix |
|---|---|
| Bill amounts may be in cents in `get` | Divide by 100 if amounts look 100x too large |
| `payment_method` only in `bills get` | Search results don't include payment method — drill into details to find ACH vs check vs wire |
| Something broken? | `ramp feedback "message"` to report CLI/API bugs |
