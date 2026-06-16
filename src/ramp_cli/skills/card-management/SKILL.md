---
name: card-management
description: |-
  List a user's cards and count or filter them by state (e.g. how many are active),
  then activate or lock/unlock a card from the terminal.
  Use when: 'list my cards', 'how many cards', 'how many active cards', 'count active cards',
  'which cards are active', 'show my cards', 'card status', 'activate my card',
  'lock my card', 'unlock my card', 'is my card active'.
  Do NOT use for: listing funds/budgets or spend allocations (use `ramp funds list`),
  agent card payments (use agentic-purchase), or transaction history (use spend-analysis).
---

## Non-Negotiables

- To list cards, use `ramp cards list` — NOT `ramp funds list` (that lists funds/budgets, not cards).
- The API has no server-side state filter. To count or filter by state, list all cards, then filter on `card_state` client-side.
- Pass `--agent` for machine-readable JSON output whenever you need to count or filter programmatically.
- `card_state` is a free-form string from the API with no fixed enum in the spec; active cards report a `card_state` that equals `active` case-insensitively (e.g. `ACTIVE`). Always compare case-insensitively and report exactly what the API returns; do not invent states.
- A card's lifecycle state (`card_state`) is distinct from its `activation_status` (whether the physical card has been activated). A card can be active while still `not_activated` for physical use.

## Workflow

### Step 1: List the cards

```bash
ramp cards list --rationale "list the user's cards" --agent
```

In `--agent` mode the response is wrapped in the standard envelope: the cards live in the nested array at `.data[0].cards[]` (not one card per `.data[]`). Each card object includes `display_name`, `last_four`, `card_state`, `activation_status`, `card_type`, `is_physical`, and `id`.

Admins and managers can target another employee's cards by passing that user's UUID — run `ramp cards list --help` to confirm the option is available in your synced spec.

### Step 2: Count or filter by state

Count active cards directly with `jq`. The cards are at `.data[0].cards[]`, and `card_state` casing is compared case-insensitively so it works regardless of how the API capitalizes it:

```bash
ramp cards list --rationale "count active cards" --agent | jq '[.data[0].cards[] | select((.card_state // "" | ascii_downcase) == "active")] | length'
```

List just the active card names:

```bash
ramp cards list --rationale "list active cards" --agent | jq -r '.data[0].cards[] | select((.card_state // "" | ascii_downcase) == "active") | .display_name'
```

In a human terminal, `ramp cards list` shows `card_state` and `activation_status` as columns, so you can count active cards by eye.

### Step 3 (optional): Activate or lock a card

Activate a physical card by its last four digits:

```bash
ramp cards activate --last_four 1234 --rationale "activate the user's new physical card" --agent
```

Lock or unlock a card by its id (the `id` field from `cards list`) — the id is a required positional argument:

```bash
ramp cards lock 7f3c0d2a-9b1e-4a55-8c21-0e9d6b2f4a10 --action lock --rationale "user lost their card" --agent
ramp cards lock 7f3c0d2a-9b1e-4a55-8c21-0e9d6b2f4a10 --action unlock --rationale "user found their card" --agent
```

The same tools are also available under `funds` (`ramp funds list-cards`, `ramp funds activate`, `ramp funds lock-or-unlock-card`) — `cards <alias>` is a convenience alias for the same endpoints.

## Fields Available (from `cards list`)

| Field | Description |
|---|---|
| `display_name` | Card name |
| `last_four` | Last four digits |
| `card_state` | Lifecycle state; equals `active` (case-insensitive, e.g. `ACTIVE`) when the card is active |
| `activation_status` | Whether the physical card has been activated |
| `card_type` | Virtual or physical card type |
| `is_physical` | Whether the card is a physical card |
| `is_locked` | Whether the card is currently locked |
| `id` | Card UUID |

## Example Session

```
User: How many active cards do I have?

Agent: > ramp cards list --rationale "count active cards" --agent | jq '[.data[0].cards[] | select((.card_state // "" | ascii_downcase) == "active")] | length'
3

You have 3 active cards.
```

## Gotchas

| Issue | Fix |
|---|---|
| `ramp funds list` returns funds/budgets, not cards | Use `ramp cards list` to list cards |
| No `--state`/`--active` flag exists | List all cards and filter on `card_state` client-side |
| `card_state` casing varies | Compare case-insensitively (`ascii_downcase` in jq) |
| Something broken? | `ramp feedback "message"` to report CLI/API bugs |
