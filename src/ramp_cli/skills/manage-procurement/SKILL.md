---
name: manage-procurement
description: |-
  Search, inspect, summarize, and safely review procurement requests and
  purchase orders. Use when: 'find a PO', 'show procurement request details',
  'purchase order status', 'what procurement requests need approval', 'approve
  this PO request'. Do NOT use for: bill payment, card transaction cleanup,
  reimbursements, or vendor document upload.
---

# Manage Procurement

Use this skill for purchase order lookup, unified procurement request lookup,
pending procurement approvals, and safe request approval or rejection.

Do not use it for bill approval/payment, card transaction cleanup,
reimbursements, vendor document upload, accounting recoding, or contract edits.

## Rules

- Use `purchase_orders` for PO lookup/detail. Use `requests` for unified request
  search, request detail, pending approval queue, approval, and rejection.
- Keep identifiers labeled. `purchase_order_id`, `request_uuid` /
  `unified_request_id`, and `spend_request_id` are distinct. Approval acts on
  the unified request UUID, not the PO UUID.
- Use `--agent` for responses you need to parse.
- Use `--json` for search filters. The CLI validates unknown JSON keys and enum
  values, so use `--dry_run --json ...` when checking payload shape.
- Use exact pagination cursors returned by the API. Do not trim or rewrite them.
- Procurement schemas do not return deep-link URLs. Present returned identifiers
  and statuses instead of inventing links.
- Do not approve or reject without first showing details and confirming the
  user's intent.
- For rejection, include the user-supplied or user-accepted reason in
  `--thoughts`.
- PO amount fields are numeric currency units with a `currency` code. Display
  them directly; do not apply cents conversion.

## Workflow

1. Search POs first when the user gives a PO number, vendor, or procurement
   record:

   ```bash
   ramp purchase_orders search --json '{"rationale":"Searching purchase orders by vendor or PO number","filters":{"search":"Figma"},"limit":10}' --agent
   ```

   If a known PO number is not returned, the spend request may still be pending
   and the PO may not be issued yet. Search unified requests scoped to purchase
   orders:

   ```bash
   ramp requests search --json '{"rationale":"Searching purchase order requests before PO issuance","filters":{"search":"Figma","unified_spend_request_types":["PURCHASE_ORDER"]},"limit":10}' --agent
   ```

   Unified request search and pending rows include `request_uuid` and may include
   `purchase_order_number`, but they do not include `purchase_order_id`. To move
   from a unified request row to PO detail, first call `requests get` with
   `request_uuid` and use `purchase_order_id` from that detail response if it is
   present.

2. Get detail before summarizing or taking action:

   ```bash
   ramp purchase_orders get {purchase_order_id} --rationale "Reviewing purchase order details" --agent
   ramp requests get {request_uuid} --rationale "Reviewing request before action" --agent
   ```

   On PO details, the request UUID is named `unified_request_id`. On request
   search, pending, detail, approve, and reject responses, it is named
   `request_uuid`. Do not call `purchase_orders get` directly from a request
   search or pending row; list rows do not expose `purchase_order_id`.

3. Review pending approvals through the request queue:

   ```bash
   ramp requests pending --rationale "Reviewing pending procurement approvals" --thoughts "Reviewing pending procurement requests" --page_size 50 --request_types PURCHASE_ORDER --agent
   ```

   Paginate with the returned cursor:

   ```bash
   ramp requests pending --rationale "Continuing pending procurement approval review" --thoughts "Reviewing pending procurement requests" --page_size 50 --request_types PURCHASE_ORDER --start "{next_page_cursor}" --agent
   ```

4. Before approval or rejection, check the current approval step:

   ```bash
   ramp requests get {request_uuid} --rationale "Pre-approval workflow check" --agent
   ```

   If `approval_workflow.needs_user_action` is not `true`, do not call
   `requests approve`; hand off with the identifiers and the current approval
   step from `approval_workflow.steps`.

5. Act only after the user confirms the exact request UUID:

   ```bash
   ramp requests approve {request_uuid} --action APPROVE --rationale "Approving confirmed purchase order request" --thoughts "Approved after confirming PO details with the user" --agent
   ramp requests approve {request_uuid} --action REJECT --rationale "Rejecting confirmed purchase order request" --thoughts "Rejected: {confirmed_reason}" --agent
   ```

## Output

For search results, keep rows compact and use fields from the command response:

```text
PO number | vendor | amount | currency | PO status | id
```

For detail summaries, include available status and relationship fields:

```text
PO number:
Vendor:
Amount:
PO status:
Request status:
Promise date:
Linked bills:
Linked transactions:
Linked item receipts:
Unified request ID:
```

Before approval, state that approval acts on the unified request UUID, not the
PO ID. After action, surface returned `request_uuid`, `action`, `success`, and
`message`.

## Handoff

Hand off instead of guessing when the CLI cannot complete the workflow, the
current user is not the active approver, the user asks for unsupported edits, or
the task belongs to bills, reimbursements, transactions, vendor documents, or
contracts.

Include the identifiers and statuses you have:

```text
PO number:
Purchase order ID:
Unified request ID:
Spend request ID:
Request status:
Current approval step:
```
