---
name: apply-to-ramp
description: |-
  Guide a user through submitting a Ramp financing application via the CLI.
  Use when: "apply to ramp", "create application", "sign up for ramp",
  "submit a ramp application", "onboard a new business".
  Do NOT use for: existing account management, transaction queries, or receipt uploads.
---

## Non-Negotiables

- **New emails preferred**: if the applicant email is already associated with a Ramp account, the API still returns success but the applicant receives instructions to sign in or continue their existing application — no new application is created. Warn the user to use a new email for a fresh application.
- **Flag syntax**: this command uses `--json` with a raw JSON body — NOT individual flags.
- **Schema source of truth**: run `ramp applications create --example` to see a complete example payload with all fields. Do NOT hardcode field names — always verify against the example output.
- **Confirm before submit**: after assembling the JSON, show the user a human-readable summary of what will be sent. Ask if they'd like to review the full JSON payload or proceed.
- **Dry-run available**: `ramp applications create --json '<body>' -n` previews without sending.
- **Browser steps remain**: phone verification (OTP), legal agreements, and bank linking must be finished in the browser. Mention this upfront AND remind the user after submission.
- **Files accepted at any step**: the user can provide a file (PDF, CSV, JSON, spreadsheet, etc.) at any point during the flow — not just at the beginning. If a file is provided mid-flow, extract what you can and merge it with what you've already collected.
- **Use structured prompting tools**: if the agent runtime provides structured user-prompting tools (e.g., AskUserQuestion, interactive prompts, selection menus), prefer them over free-form text questions. Batch related choices into a single prompt when possible.

## Test vs Real Application

Ask the user upfront whether they want to submit a **test application** or a **real one**.

- **Test application**: switch to sandbox first with `ramp env sandbox`. No real account is created and nothing goes through underwriting.
- **Real application**: ensure production environment with `ramp env production`. Submits a real financing application — the applicant receives an actual invite email and goes through underwriting.

Check current environment with `ramp env`. Switch with `ramp env sandbox` or `ramp env production`.

## Required Fields

The API only enforces `applicant` (email, first_name, last_name) — but the full application requires much more to be approved. The fields below are **required for approval**. Any you don't collect via the CLI, the applicant will need to fill in the browser.

| Section | Field | Required for Approval |
|---------|-------|-----------------------|
| **applicant** | `email`, `first_name`, `last_name` | **Yes** (API-enforced) |
| **applicant** | `phone` | **Yes** (verified via OTP in browser) |
| **business** | `business_name_legal`, `business_website`, `business_description` | **Yes** |
| **business** | `phone` | **Yes** |
| **business.address** | `street_address`, `city`, `state`, `postal_code` | **Yes** |
| **business.incorporation** | `entity_type`, `date_of_incorporation`, `state_of_incorporation`, `ein_number` | **Yes** |
| **financial_details** | `estimated_monthly_spend_amount` | **Yes** |
| **financial_details** | `estimated_monthly_ap_spend_amount` | Conditional (only if applying for Ramp Bill Pay) |
| **controlling_officer** | `first_name`, `last_name`, `email`, `phone`, `birth_date`, `title` | **Yes** |
| **controlling_officer.address** | `street_address`, `city`, `state`, `postal_code`, `country` | **Yes** |
| **controlling_officer** | `ssn_last_4` or `passport_last_4` (one of) | **Yes** |
| **controlling_officer** | `is_beneficial_owner` | **Yes** |
| **beneficial_owners[]** | Same fields as controlling officer | **Yes** (for each 25%+ owner) |
| **business** | `business_name_dba`, `business_name_on_card` | No |

## Workflow

### Step 1 — Get the schema

```bash
ramp applications create --example
```

This prints a complete example JSON payload with all fields and their types.

### Step 2 — Set expectations

Ask two things upfront:
1. "Would you like to submit a test application or a real one?"
2. "For a test application, I can prefill example data for everything except your email (which must be real to receive the invite). Want me to use example data as defaults, or would you prefer to enter everything yourself?"

If the user opts for example data, use the example values from the `--example` output as defaults for all non-email fields. The user can still override any field.

Then tell the user:
> I'll collect your information for the Ramp application. The more you provide now, the less you'll need to fill in later. After submitting, you'll receive an email to finish a few remaining steps in the browser — phone verification, legal agreements, and bank linking.
>
> You can also provide a file at any point (PDF, CSV, JSON, spreadsheet, etc.) and I'll extract what I can from it.

### Step 3 — Collect information

The user can provide information conversationally, via a file, or both. At every section, offer the option to provide a file. If a file is provided at any point, read it, extract every field you can map to the schema, show what you found, and continue filling gaps.

Compare what you have against the Required Fields table above. Collect sections in this order:

1. **Applicant** — always collect a real email, even for test applications. Also collect first_name, last_name, phone.
2. **Business** — legal name, website, description, phone, full address, incorporation details (entity_type, date, state, EIN).
3. **Financial details** — estimated monthly card spend. Ask if they're also applying for Ramp Bill Pay — if yes, also collect estimated monthly AP spend.
4. **Controlling officer** — first_name, last_name, email, phone, birth_date, title, full address, ssn_last_4 or passport_last_4. Ask: "Is the controlling officer also a beneficial owner (owns 25%+ of the business)?" If yes, set `is_beneficial_owner: true`.
5. **Beneficial owners** — ask if there are additional individuals who own 25%+ of the business. If the controlling officer is already marked as a beneficial owner, they don't need to be listed again. For each additional owner, collect the same fields.

For each section:
- Batch related fields together (e.g., ask for full address at once, not street/city/state separately).
- Offer "I have a file" as an option alongside conversational input.
- If the user wants to skip a section, note they can finish it in the browser after submitting.

Skip `oauth_authorize_params` entirely — it's for partner integrations, not direct applicants.

### Step 4 — Assemble and confirm

Build the JSON body from collected fields. Show the user a readable summary:

```
Applicant:   Jane Doe (jane@acme.com, +14155550124)
Business:    Acme Plumbing LLC — 123 Market St, San Francisco, CA 94105
             acmeplumbing.com — "Residential and commercial plumbing services"
EIN:         12-3456789 (LLC, incorporated CA 2018-06-15)
Officer:     Jane Doe — Owner (also beneficial owner)
Spend est:   $50,000/mo card, $25,000/mo AP
```

Ask: "Does this look correct? Want to review the full JSON, make changes, or submit?"

### Step 5 — Submit

```bash
ramp applications create --json '<assembled_json>' --agent
```

On success, tell the user:
> Application submitted! You'll receive an email to verify your address. From there, you'll finish up in the browser:
> - Verify your phone number
> - Accept legal agreements
> - Link a bank account

## Example Session

```
User: I want to apply to Ramp for my business

Agent: I can help with that. Two quick questions:
1. Would you like to submit a test application or a real one?
2. If test, want me to prefill example data (you just provide your email)?

User: Real application

Agent: > ramp env production
> ramp applications create --example

I'll collect your information. The more you provide now, the less you'll
need to fill in the browser later. You'll need to finish phone verification,
legal agreements, and bank linking in the browser after submitting.

Let's start with the applicant. What's your name and email?

User: Jane Doe, jane@acmeplumbing.com, phone is 415-555-0124

Agent: Got it. Now the business details — legal name, website, description,
phone, and full address?

User: Acme Plumbing LLC, acmeplumbing.com, "Residential and commercial
plumbing services", same phone. 123 Market St, San Francisco CA 94105

Agent: Incorporation details — entity type, date, state, and EIN?

User: LLC, incorporated June 2018 in California. EIN 12-3456789

Agent: Monthly card spend estimate? Also, are you applying for Ramp Bill Pay?

User: About $50k/month on cards, yes to Bill Pay, maybe $25k/month AP

Agent: Last section — controlling officer. Is that you? And do you own 25%+?

User: Yes and yes, I'm the sole owner. DOB 1985-03-15, SSN last 4: 1234

Agent: Here's the summary:

Applicant:   Jane Doe (jane@acmeplumbing.com, +14155550124)
Business:    Acme Plumbing LLC — 123 Market St, San Francisco, CA 94105
             acmeplumbing.com — "Residential and commercial plumbing services"
EIN:         12-3456789 (LLC, incorporated CA 2018-06-15)
Officer:     Jane Doe — Owner (sole beneficial owner)
Spend est:   $50,000/mo card, $25,000/mo AP

Does this look correct?

User: Yes, submit it

Agent: > ramp applications create --json '{...}' --agent
Application submitted! Check your email at jane@acmeplumbing.com to continue.
```

## When NOT to Use

- **Existing Ramp accounts** — this is for new applications only
- **Transaction queries or receipt uploads** — use other Ramp CLI skills
- **Account management or settings** — use the Ramp dashboard

## Gotchas

| Issue | Fix |
|-------|-----|
| Phone numbers must be E.164 format | Prefix with +1 for US numbers, e.g., `+14155550123` |
| SSN/passport fields are last-4 only | Do NOT ask for full SSN — only the last 4 digits. One of SSN or passport is required. |
| `entity_type` is an enum | Valid values: `COOPERATIVE`, `CORPORATION`, `LLC`, `NON_PROFIT_CORPORATION`, `OTHER`, `PARTNERSHIP`, `SOLE_PROPRIETORSHIP` |
| Dates must be YYYY-MM-DD | `birth_date`, `date_of_incorporation` — reformat if user gives other formats |
| `state` / `state_of_incorporation` are 2-letter codes | Convert full state names to abbreviations |
| Amounts are in whole dollars | `estimated_monthly_spend_amount` is dollars, not cents |
| Existing email returns success but no new app | The API returns success, but the applicant gets sign-in/continue instructions instead of a new application. Warn the user to use a new email for a fresh application |
| CO and BO overlap | If the controlling officer owns 25%+, set `is_beneficial_owner: true` on the officer — don't duplicate them in `beneficial_owners` |
| The API response body is not stable | Don't try to parse the response — just check for success/failure |
| Something broken? | `ramp feedback "message"` to report CLI/API bugs |
