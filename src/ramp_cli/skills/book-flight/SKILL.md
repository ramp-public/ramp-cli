---
name: book-flight
description: "Books flights conversationally through the ramp CLI: resolves cities to airports, searches one-way and round-trip flights, presents and compares offers, previews the fare, and tickets the booking on the traveler's explicit approval. The user describes a trip in plain language ('book a flight from Toronto to SFO') and never needs to know a CLI command. Use when someone wants to book, find, search, or compare flights, or says 'fly from X to Y'. Not for cancellations, changes, refunds, seat selection, loyalty programs, hotels, cars, or multi-city trips."
---

# Book a Flight (conversational flight search)

The user describes a trip in plain words. Turn that into `ramp travel` commands, run them,
and show clean results. **Never show or ask the user to type a CLI command** — talk like a
travel helper ("Searching Toronto → San Francisco, Jul 1…"), not about flags.

The Steps, Phases, checklist, and flag names in this guide are **your** internal scaffolding —
never surface them to the user. Don't say "Phase 1," "Step 3," or name flags; just narrate in
plain travel language ("Let me pull up the fare and check it before booking…").

## Prerequisites

- `ramp` CLI installed and logged in (`ramp auth login`). Run where `ramp` works (or
  `uv run ramp` inside the ramp-cli repo).

## Scope

- ✅ Resolve cities to airports; search one-way and round-trip; show offers.
- ✅ Round-trip: fetch the matching **return** flights for the outbound the user picks (Step 5).
- ✅ **Book the ticket** — preview, confirm only on an explicit yes, then verify (Step 6).
  Booking books for **you** (the logged-in user) and spends **real money**.
- ✅ Compare cabins/fares — **only when asked** (see "Comparing cabins or fares").
- ✅ Read or update the traveler's profile, and read trips and bookings (see "Supporting tools").
- ❌ Booking for someone else. If asked, say you can only book for the signed-in user.
- ❌ Cancellations, changes, refunds, seat selection, loyalty, hotels, cars, multi-city —
  those happen in the Ramp web app.

## Rules for every command

- **Always `--output json`** on every `travel search-flight` (including the Step 5 return search).
  Read the JSON and build a friendly table; don't show raw JSON unless asked, and don't pipe
  through `python`/`jq`. The text output can change — JSON is the stable format.
- Every command needs a `--rationale`. Once the trip is known, **name it in every related
  command's rationale** (route + dates, e.g. "Toronto→SFO, Jul 1-8") and keep that reference
  consistent across the whole flow — search, returns, preview, book, fund/spend-allocation,
  and verify. Rationales are logged, so a consistent trip reference makes a trip's commands
  easy to group and its intent easy to read later.
- `--departure`/`--arrival` each take **one** value: an airport code (`SFO`) or a Ramp city
  id (`search_code` from `travel locations`, Step 2). No lists.

**Don't over-specify.** These default off — add each only when the user asks:

- **`--cabin_class`** — off searches all cabins; **don't default to Economy**. It only filters
  the cabin — either way each offer comes back as a single cheapest fare (`price` +
  `fare_name`), **no `fare_options`**. Add it only when the user names a cabin.
- **`--limit`** — off returns a default page; paginate with `next_cursor` (Step 3). Add only
  for "just show me 3".
- **`--sort_key`** — off uses `WEIGHTED_SCORE` (a good blend). Other keys:
  `LOWEST_TOTAL_AMOUNT`, `SHORTEST_DURATION`, `LEAST_NUMBER_OF_STOPS`,
  `EARLIEST_DEPARTURE_TIME`, `LATEST_DEPARTURE_TIME`, `EARLIEST_ARRIVAL_TIME`,
  `LATEST_ARRIVAL_TIME`. Sorting applies to a new search only — re-sort by starting fresh,
  not on a `--resume_job_id` page.
- **`--include_fare_options`** — off. It's the **only** thing that returns `fare_options`; any
  search without it (cabin or not) has none. Add it only to compare cabins/fares.

- **Use `ramp travel search-flight`** for flight searches. Check `ramp travel --help` if the
  alias is unavailable before continuing.

## Step 1 — gather trip details

Use what the user gave you; infer the rest. Only ask about things both missing and
important, and ask them all at once (never one at a time).

Infer silently, then say back (don't ask):

| Slot | Assume |
|---|---|
| **Trip type** | round-trip if there's a return date, "back on…", or a stay length; else one-way. Ask only if truly unclear. |
| **Relative dates** | resolve to `YYYY-MM-DD`. **Always say the date back** so mistakes surface before money moves. |
| **Airport given** | `SFO`, `JFK`, etc. → use directly, skip Step 2. |
| **Cabin** | none (leave `--cabin_class` off). |

Say assumptions in one line as you go — *"Searching JFK → SFO, Mon Jul 6, round-trip…"*.

If something required is still missing, ask for all of it in a single `AskUserQuestion`
(selectable options, one question per item). Required: **destination**, **origin** (if no
home airport to guess), **departure date**, **one-way vs round-trip** (if unclear), **return
date** (round-trip). Keep dates in the future — 14+ days out is safest (a common policy
cutoff). Never re-ask what they told you.

## Step 2 — resolve a place to a `--departure`/`--arrival` value

- **Airport code** (`SFO`, `JFK`) → use directly, skip this step.
- **City/vague place** ("New York", "the Bay Area") → look it up first:

```bash
ramp travel locations --query "New York" --location_type city --limit 5 \
  --rationale "resolve New York to a metro id for the user's trip" --output json
```

For a **city**, the metro id is its **`search_code`** (a UUID; `iata_code` is empty). Pass
that one `search_code` to search the whole metro (New York covers JFK/LGA/EWR; Toronto covers
YYZ/YTZ). For one specific airport, search `--location_type airport` and pass its `iata_code`.

## Step 3 — search flights

Add `--return_date` only for round-trips. No `--cabin_class`/`--limit`/`--sort_key` unless
the user named a cabin, count, or order.

```bash
ramp travel search-flight --output json \
  --departure YYZ --arrival SFO \
  --departure_date 2026-07-01 --return_date 2026-07-08 \
  --rationale "search flights for the Toronto→SFO trip, Jul 1-8"
```

**Usually one call returns the full ranked set** — `offers` come back with `in_policy`
already worked out, no client-side polling.

**Check `search_complete` first.** On a dense route the server can return
`search_complete: false` with only the best offers so far and no cursor. When that happens,
re-call `search-flight` with `--resume_job_id <job_id>` (this response's `job_id`, no `--cursor`);
the workflow keeps running, so the resume usually returns the final, ranked set. Repeat until
`search_complete: true`, then build the table. The resumed offers **supersede** the partial
ones (same ids) — replace, don't append. Don't present partial offers as final or tell the
user to change their trip mid-search.

Once complete, empty `offers` = no match — tell the user and offer to change dates/airports.
(Output is also token-capped, so a long result may page: pass `job_id` as `--resume_job_id`
and `next_cursor` as `--cursor` to fetch more, only if the user wants beyond the first page.)

**For round-trips, save this response's `job_id`** — it's `--search_job_id` in Step 5.

## Step 4 — show the offers

Turn the JSON into **one table**. Keep each offer's `id` out of the table (you need it for
returns/booking). Each `offers[]` item has exactly these keys (don't invent others): `id`,
`airline_name`, `flight_number`, `departure_airport`/`arrival_airport`,
`departure_time`/`arrival_time`, `departure_date`/`arrival_date` (for the `⁺¹` next-day mark),
`duration`, `stops`, `price`, `in_policy`/`policy_reason`, `fare_name` (free-text fare label),
and `fare_options` (the per-fare grid — **present only when you passed `--include_fare_options`**).

| # | Airline | Flight | Depart → Arrive | Duration | Stops | Price (round-trip total) | Policy |
|---|---------|--------|-----------------|----------|-------|--------------------------|--------|
| 1 | JetBlue | B6 0115 | 6:00 AM → 9:15 AM | 6h 15m | Nonstop | **$289** | ✓ |

- **#** — the row's on-screen position (top = `1`, no gaps), not the JSON index. If you
  reorder (e.g. cheapest in-policy first), renumber top to bottom. Keep a private `#`→`id`
  map so "book #3" resolves correctly.
- **Depart → Arrive** — local times; add `⁺¹` when arrival is next-day.
- **Duration**, **Stops** — as returned (`Nonstop`, `1 stop`).
- **Price** — always show. Round-trip header says **"round-trip total"** (covers both legs);
  one-way says **"Price"**. Say which in words.
- **Policy** — `in_policy: true` → **✓**, `false` → **✗** + short `policy_reason` (e.g.
  *✗ (booked < 14 days out)*), `null` → **—** (not checked); never show **✗** for `null`.

Above the table, show up to 3 recommended options with a couple-word reason, price, and policy
status (*"Recommended: JetBlue — cheapest in-policy, $289 round-trip."*). If
`search_policy_summary` has text, show it once as a short banner.

## Step 5 — round-trip: confirm the outbound, then fetch returns

Round-trips only. **Wait for the user to name the outbound.** Don't guess or default to
cheapest/first. Ask *"Which outbound do you want? I'll pull the matching returns once you
pick."* If vague ("the morning one") and more than one fits, confirm the exact flight.

The return step is a **second flight-search call** — same command, with the chosen outbound's
`id` as `--outbound_offer_id` plus the Step 3 `job_id` as `--search_job_id` (carries outbound
context for return-policy). Don't pass `--departure`/`--arrival`/dates again — mixing them
with `--outbound_offer_id` is rejected.

```bash
ramp travel search-flight --output json \
  --outbound_offer_id "<chosen_outbound_offer_id>" \
  --search_job_id "<job_id_from_step_3>" \
  --rationale "return offers for the chosen outbound, Toronto→SFO trip Jul 1-8"
```

The response is `is_round_trip: true` with `offers` being the return legs. (Return mode is
synchronous, so its `job_id` is null; page more returns by reusing `--outbound_offer_id` +
`--cursor`.) Show them like Step 4. **Each return offer's price is the full round-trip
total** — say so (*"the nonstop keeps your trip at $289; the 1-stop return makes it $396
total"*). Returns often come back `in_policy: null`; if every offer is null, drop the Policy
column (never show ✗ for null). The id you carry to booking is the chosen **return** offer's
`id`.

## Step 6 — book (preview → confirm → verify)

Always three steps; never book in one shot, never assume a yes, never book a flight the
traveler didn't name. Booking spends **real money**.

Before previewing or confirming a booking, check whether the traveler already has a Ramp
travel profile:

```bash
ramp travel profile --output json \
  --rationale "check whether the Toronto→SFO Jul 1-8 trip traveler profile is ready for booking"
```

If `has_profile` is `false`, collect the required traveler details in one message, then update
the profile before continuing. Use the tool to save the details the traveler gives you; do not
send them to the Ramp web app for this.

```bash
ramp travel profile-update --output json \
  --first_name "Taylor" \
  --last_name "Smith" \
  --date_of_birth "1990-01-15" \
  --email "taylor@example.com" \
  --phone_number "+14155550123" \
  --rationale "create the traveler profile needed to book the Toronto→SFO Jul 1-8 trip"
```

Only continue when the update succeeds. Confirm the profile-update result reports success, or
re-run `travel profile` to verify the traveler now has a profile before moving on. If the update
fails, correct the missing details and retry instead of continuing to booking.

Once the profile exists, continue to the normal preview, confirmation, and verification flow.

**Which id:** one-way → the chosen offer's `id` from `search-flight`; round-trip → the chosen
**return** offer's `id` from Step 5 (it represents the whole round-trip and its both-legs
total — **not** the outbound id). Pass it as the first arg (`ramp travel book "<id>"`).
Behind it is **`flight_offer_uuid`**, so a `--json` body uses key `flight_offer_uuid`, not
`offer_id`.

### Phase 1 — preview (always first)

Run `book` **without `--confirm`** — that returns the preview and books nothing.

```bash
ramp travel book "<flight_offer_uuid>" --output json \
  --rationale "preview fare for the Toronto→SFO Jul 1 trip before the traveler confirms"
```

Show plainly: route/dates, airline/flight, cabin/fare, **total**, policy result, and paying
fund(s). The preview returns **`eligible_funds`** (each with `fund_uuid`, `fund_name`,
sometimes `available_balance`/`spending_limit`).

**Always ask: *"Want to pay from a specific fund?"*** (a yes/no). Then:

- **Yes** → pass the chosen fund's `fund_uuid` as `--spend_allocation_id`. One eligible fund →
  name it and use it; several → list them by name and ask which.
- **No** → leave `--spend_allocation_id` off. This is **not** an auto-pick — it **requests a
  new allocation** (a fresh spend request that goes through approval). That's a fine choice;
  just be clear that's what happens, don't call it "auto-selected."
- **Empty `eligible_funds`** → there's no fund to specify; omit `--spend_allocation_id` and a
  new allocation is requested.

Whether the booking needs sign-off comes from the preview's **`requires_approval`** /
**`approval_steps`** — surface those plainly rather than inferring it from the fund.

Then state the total in plain words and **ask for a clear yes** — *"This books LHR → JFK on
Delta, Jul 6, for **$412 total**, paid from the Travel fund. Book it?"* Stop and wait.

### Phase 2 — confirm (only after a clear "yes")

Add `--confirm` and pass the preview's total as `--expected_total_amount` (rejects the
booking if the fare moved instead of quietly charging more). Use the preview's `total_amount`
**exactly as a string with the currency symbol**, **single-quoted** (`'$288.80'`) — double
quotes let the shell eat `$2`, sending `88.80` and triggering a false price-change rejection.

```bash
ramp travel book "<flight_offer_uuid>" --confirm \
  --expected_total_amount '<preview_total_amount>' --output json \
  --rationale "book the Toronto→SFO Jul 1 trip; traveler approved the previewed fare"
```

Extra flags, only when they apply:

- **`--spend_allocation_id <fund_uuid>`** — the chosen fund's `fund_uuid` when the traveler
  specified one. Omit it if they chose not to (or `eligible_funds` was empty) — that requests
  a new allocation.
- **`--reason "<note>"`** — booking notes, if offered.
- **`--trip_id <uuid>`** — attach to an existing trip; off to auto-pick/create.

Price-change error = fare moved: re-run Phase 1 and re-confirm only after the traveler
re-approves. (`--dry_run` prints the request without sending.)

### Phase 3 — verify it went through

**The confirm response is optimistic, not final** — it can say `approved`/`pending_approval`
and still fail in fulfillment. Don't say "you're booked" off the confirm alone:

```bash
ramp travel bookings --include_flights --output json \
  --rationale "verify the Toronto→SFO Jul 1 booking reached a terminal status"
```

Find the flight you just booked by matching what `travel bookings` returns — departure/arrival
airports, `flight_number`, and departure time. If several match (e.g. earlier pending/failed
attempts on the same route/date), pick the most recent by **`booked_at`** — that's the one you
just created. (`travel bookings` doesn't return the confirm's `booking_request_id`, so don't
match on that.) Report its `status`. Most read for themselves (`CONFIRMED`, `PENDING_APPROVAL`,
`CANCELLED`). Two need care:

- **`PROCESSING`** is **not final** — wait and re-run `travel bookings` until it settles;
  don't report it as booked yet.
- **`FAILED`** — show `error_message` exactly. If it points to missing traveler details, use
  `travel profile` and `travel profile-update` to complete the profile before retrying.

## Comparing cabins or fares (only when asked)

Only when the user asks to compare cabins, see upgrade prices, or asks about fare differences
("show the class options", "how much to upgrade to business"). A normal search returns **no**
fare grid, and you **can't** get one by resuming a prior lean search — when the user wants to
compare, run a **fresh search with `include_fare_options=true`** (then fetch returns from that
job).

- **`cabin_class`** is optional — leave off to search all cabins; add only for one cabin.
- **`include_fare_options: true`** adds each flight's bookable fare classes to `fare_options`
  (each with `fare_name`, `fare_category` — `Basic Economy`/`Economy`/`Economy Plus`/
  `Premium`/`Business`/`First` — `price`, `in_policy`/`policy_reason`, and a bookable fare
  `id`).

The offer's top-level `price` is the **cheapest** fare; `fare_options` lists the upgrades. To
book a specific fare, pass that fare option's `id`, **not** the offer's top-level `id`.

Send it via `--include_fare_options` if the available flight-search command lists it; otherwise via
the `--json` body:

```bash
ramp travel search-flight --output json --json '{
  "departure": "JFK", "arrival": "SFO",
  "departure_date": "2026-07-06", "return_date": "2026-07-10",
  "include_fare_options": true,
  "rationale": "compare cabin/fare classes for the JFK→SFO trip, Jul 6-10"
}'
```

Once you're in a comparison (the search that set `include_fare_options=true`), re-send
`include_fare_options` on every follow-up call (pagination and the Step 5 return search) — it
doesn't persist on its own — so the fare grid stays on across pages.

Present a **single cabin-grid matrix** — one row per flight, one column per cabin category —
ordered by departure time (a comparison, not a ranked list):

| # | Depart → Arrive | Airline / Flight | Basic Econ | Economy | Econ+ | Premium | Business |
|---|-----------------|------------------|-----------|---------|-------|---------|----------|
| 1 | 7:00 AM → 9:49 AM | DL 0742 | **$154** | **$209** | $354 | $759 | $3,849 |

- **#** — display position (top = `1`); keep an internal `#`→per-cabin-fare-`id` map so
  "book #2 in economy" resolves to the right fare option `id`.
- **Depart → Arrive** — local times, `⁺¹` for next-day.
- One column per `fare_category`; cell = that fare's `price`, `—` if the flight doesn't sell it.
- **Bold = in policy** (`in_policy: true`); plain otherwise; render `null` plain (don't claim
  out-of-policy). Add a one-line legend.

Above the table, give the lead line (cheapest fare anywhere + the highest in-policy cabin) and
make the upgrade math explicit (*"Basic Economy $289, in policy; Economy $399; Business
$1,101, out of policy"*).

## Supporting tools (profile, trips, bookings)

Four supporting tools; use when relevant, not on every booking.

- **`travel profile`** — the traveler's saved profile (name, email, phone, DOB, gender,
  KTN/TSA, redress, loyalty). Use for "what's my known traveler number?" and before booking to
  check whether `has_profile` is true.
- **`travel profile-update`** — saves missing traveler details before booking when
  `travel profile` returns `has_profile: false`, or when a failed booking points to missing
  traveler details.
- **`travel list`** — the traveler's trips (`--status completed|ongoing|upcoming`,
  `--cursor`). Each has `id`, `trip_name`, dates, locations. Use to find a trip `id` for
  `--trip_id` on `book`.
- **`travel bookings`** — existing flight/hotel bookings (`--include_flights`/
  `--include_hotels`, `--limit`). Each has a `status` (`CONFIRMED`/`PENDING_APPROVAL`/`FAILED`
  + `error_message`), route/times, `trip_name`/`trip_id`. **Source of truth for whether a
  booking succeeded** (Phase 3) and for "what flights do I have booked?".

## Gotchas

- Offers expire from the cache. If a return or book call fails with a cache/offer error,
  re-run the search (Step 3) for fresh offers and continue.
