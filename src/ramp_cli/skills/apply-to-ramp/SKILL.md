---
name: apply-to-ramp
description: |-
  Start a new Ramp financing application or help complete an existing
  application for an OAuth-authorized business. Use for application signup,
  progress, missing data, bank information, documents, follow-ups, and
  applicant or Ramp handoffs.
---

# Ramp Financing Application Workflow

Use `ramp applications create` for the signup flow and the generated
`ramp applications` operations for an OAuth-authorized business's existing
application. The unified agent-tool OpenAPI document defines available
operations, fields, accepted values, and OAuth scopes. Developer API responses
are canonical for current workflow state, actor ownership, and validation.

## Choose The Flow

Determine whether the human wants to start a new application or continue an
existing one. Ask if the intent is unclear.

Use `production` by default. Only use `sandbox` when the human explicitly asks
for a test application or test workflow. The command examples below use
`--env production`; substitute `--env sandbox` for that explicitly requested
test flow.

For a new application:

1. State that production starts a real signup and underwriting process. If the
   human requested a test, state that the workflow will use sandbox instead.
2. Inspect the current create schema and example:

   ```bash
   ramp applications schema --env production --agent
   ramp applications create --env production --example --agent
   ```

3. Assemble a payload using only facts supplied or explicitly confirmed by the
   human. The live schema is canonical for fields and validation. The example
   illustrates the payload shape; its values are not defaults.
4. Warn that if the applicant email already belongs to a Ramp account, a
   successful request sends sign-in or continuation instructions instead of
   creating a fresh application.
5. Show a human-readable summary that includes the selected environment and
   obtain explicit confirmation.

6. After confirmation, start the signup flow in that same environment:

   ```bash
   ramp applications create --env production \
     --json '<confirmed payload>' --wait_for_auth --agent
   ```

A successful request sends the applicant an email while the CLI waits for the
localhost OAuth callback. Keep the command running while the applicant accepts
the invite. If the callback succeeds, the CLI saves scoped credentials for the
selected environment. Tell the applicant exactly how to continue:

- If they do not have a Ramp account, choose **Create account**, finish account
  creation, then choose **Start application**.
- If they already have a Ramp account, sign in if prompted, then choose
  **Start application**.
- Once those steps are complete, return to the agent to continue.

If the command reports `authenticated: false`, the application was still
created. Run the returned scoped `ramp auth login` fallback, then continue with
the existing-application workflow below.

For an existing Ramp email, this continues the associated application rather
than creating a fresh one. Starting the application is not final submission. Do
not enter the progress workflow unless a Developer API connection is authorized
for the intended business and the human wants the agent to continue its existing
application.

For an existing application, inspect the current authorization:

```bash
ramp auth status --env production --agent
```

Reuse the current token when it already includes `applications:read`,
`applications:write`, and `bank_accounts:read`. Otherwise, authenticate the
intended business with only the scopes needed for this workflow:

```bash
ramp auth login --env production \
  --auth-level business \
  --scope applications:read \
  --scope applications:write \
  --scope bank_accounts:read
```

Any sanctioned connection user may complete the business-scoped browser
authorization. If Ramp presents a business chooser, tell them to select the
intended application business. After authorization, read the current
application and confirm that it is the intended business before making any
change.

The scoped login replaces the token stored for that environment. A later
unscoped `ramp auth login` restores access to the broader CLI tool set. After
authorization, use the control loop below. Never call `create` to update or
continue an application.

## Safety Rules

- For the existing-application workflow, the OAuth connection must authorize the
  intended business. Confirm that the returned application belongs to that
  business before changing it.
- Never invent, estimate, default, or infer application facts.
- Request and handle only the minimum information required by the current API
  action. Do not collect application data speculatively.
- Treat identity work according to the current action's actor and the nature of
  the step. When an identity-document follow-up has `actor=AGENT`, the agent may
  transmit the exact file the applicant selects and explicitly authorizes for
  that follow-up. Do not choose the evidence, make an identity judgment, inspect
  unrelated contents, or imply that transmission verifies the person. When an
  identity action has `actor=APPLICANT`, explain the request and provide its Ramp
  link. Never ask the applicant to place full government ID numbers, OTPs, or
  bank credentials in chat, command arguments, filenames, or agent-readable
  notes.
- Do not repeat sensitive values in summaries, logs, filenames, or status
  updates. Mask values when confirming them, for example `***6789`.
- Do not use `--dry_run` with a sensitive payload because dry-run output includes
  the request body.
- Never choose or infer an acknowledgement value. Transmit
  `ownership_acknowledgement` only after explicit human confirmation immediately
  before the request. Do not complete applicant-owned legal agreements or
  attestations.
- Never make identity attestations or complete applicant-driven identity
  verification, final review, or final application submission. Transmitting an
  explicitly authorized identity document for an `actor=AGENT` follow-up is
  allowed; Ramp remains responsible for evaluating it.
- If the agent runtime provides sanctioned access to the applicant's browser,
  email, or SMS and the applicant authorizes its use, the agent may assist with
  additional applicant-owned handoff steps through those channels. This is
  outside the Developer API workflow and is not officially supported; do not
  assume such access exists or weaken confirmation and privacy requirements.
- Never bypass applicant-owned flows or call underlying third-party links.
- Stop when an action has `actor=RAMP`; explain that Ramp is reviewing or
  processing the application.
- Treat `ready_for_submission=true` as readiness only. The applicant must review
  and submit the application in Ramp.
- When discussing a non-Ramp-owned action with a `deep_link_url`, offer to help
  complete everything the agent is permitted and able to handle, and also
  explain that the applicant can complete the step directly in Ramp using the
  link. Present this choice at the relevant intake or handoff point rather than
  interrupting unrelated agent work. If the link fails or may be stale, fetch
  the resource again and use the newly returned URL.

## Control Loop

For an existing application, always begin with:

```bash
ramp applications progress --env production --agent
```

Inspect `status`, `required_actions`, `optional_actions`, and
`ready_for_submission`.

Inspect the complete required-action list before acting:

1. If any action has `actor=RAMP`, do not mutate the application. Explain that
   Ramp owns the next step and stop.
2. Complete independently actionable `actor=AGENT` items one at a time through
   generated `ramp applications` operations. When an item requires applicant
   input, offer agent assistance and, when available, direct completion through
   its Ramp link. Re-fetch progress after every meaningful mutation and
   re-evaluate the entire action list before continuing.
3. After no independently actionable agent item remains, explain each
   `actor=APPLICANT` action using `guidance` and `applicant_action`. Include the
   current `deep_link_url` when present. If no link is returned, say that
   explicitly instead of inventing one.
4. Present relevant `optional_actions` separately and label them optional.
   Include every current `deep_link_url` they provide, even though optional
   actions do not block required work.

Do not stop merely because an applicant-owned item appears before an agent-owned
item in the response. Optional applicant actions do not block required agent
work.

Use `page_key`, `section_key`, `followup_reasons`, follow-up prompts,
`accepted_document_types`, and command help generated from OpenAPI descriptions
to determine what information is missing. Re-run progress after every meaningful
PATCH, document change, bank-account change, or follow-up submission.

Continue until no independently actionable agent work remains, Ramp owns the
next step, the applicant must act, the status is terminal (`APPROVED`,
`REJECTED`, or `WITHDRAWN`), or there is no further actionable work.

## Information Intake And Privacy

When information is missing, explain what is needed, why the current action
needs it, and where the applicant can usually find it. Offer these intake
options:

1. The applicant provides or confirms the specific value.
2. The applicant provides a relevant file and authorizes its use for the current
   application task.
3. The applicant optionally consolidates relevant files in a private local
   folder for the agent to inspect.
4. The applicant completes the step in Ramp when a current `deep_link_url` is
   available or the value should not enter agent context.

When multiple agent-owned sections need source material, proactively offer to
set up the optional working folder instead of only asking the applicant to
provide files. Explain that the agent can create a minimal intake checklist and
organize intentionally provided source documents by section. Ask the applicant
to choose or approve the location before creating anything, and continue
without a folder if they decline.

Batch related, non-sensitive questions so the applicant can answer efficiently.
Keep sensitive requests separate and ask only when the current action requires
them.

If using an optional working folder:

- Ask before creating or reading it. Let the applicant choose the location.
- Recommend a private location outside source repositories and shared project
  folders. Check that it is not tracked by Git before writing anything.
- Offer to create a concise checklist from the current progress response, with
  one entry per missing section, suggested source documents, and completion
  status. Do not place application values or sensitive identifiers in it.
- Create only the folders or organizational files the applicant approves. Use
  clear section names such as `formation`, `ownership`, and `financials`; do not
  require a fixed layout.
- Use it only for files the applicant intentionally makes available. Do not scan
  parent directories or unrelated files.
- Keep a checklist of missing sections and relevant filenames, not a duplicate
  database of extracted application values.
- Do not copy, rename, or reorganize source files without permission.
- Do not write assembled request JSON, raw extracted PII, credentials, or OTPs
  to disk. Remove agent-created temporary extracts after use; never delete the
  applicant's source files.

Practice data minimization when reading files:

- Prefer the applicant directly providing or confirming a specific value over
  reading an entire document.
- When the execution environment supports human-populated secret variables or
  masked input, prefer passing the value through without printing, logging, or
  separately inspecting it. Do not imply blind handling when the CLI or API
  requires the agent to receive the value.
- When a file is necessary, inspect only the relevant file, pages, tables, or
  fields. Avoid displaying or summarizing unrelated contents.
- Treat authorization to read a file as limited to the stated application task.
- Ask the applicant to redact unrelated sensitive information when practical.
- If the API requires a value in a CLI request, be transparent that the agent
  must receive that value to transmit it. Do not claim blind handling. Offer an
  applicant-owned Ramp flow instead when available.

Common sources:

- EIN and legal name: IRS EIN confirmation letter, tax filings, or formation
  records.
- Incorporation date, state, and entity type: articles or certificate of
  incorporation or organization.
- Ownership and beneficial owners: cap table, ownership ledger, operating
  agreement, or formation records.
- Bank routing and account numbers: bank statement, check, or authenticated
  online banking. Prefer applicant entry when unnecessary account activity would
  otherwise be exposed.
- Balances and financial figures: recent bank statements, accounting system,
  balance sheet, or profit-and-loss statement.
- Addresses: current official records.
- Identity details and documents: use an applicant-owned Ramp flow when the
  current action assigns it to the applicant. For an agent-owned document
  follow-up, offer either direct applicant completion when available or
  transmission of an explicitly selected and authorized file.

This guidance is advisory. Dynamic API requirements and validation remain
canonical. Do not turn these examples into static requirements.

## Application Updates

Read the current application before requesting or applying updates:

```bash
ramp applications get --env production --agent
ramp tools schema applications edit --env production --agent
ramp applications edit --env production --help
```

Ask only for facts needed by the current required action. PATCH only fields
supplied or explicitly confirmed by the human or an authoritative source.
Preserve partial-update semantics by omitting every unknown field.

```bash
ramp applications edit --env production \
  --json '<source-backed partial object>' --agent
ramp applications progress --env production --agent
```

Present Developer API validation details as recoverable input issues. Ask the
human to correct the specific rejected value; do not work around validation.

If progress still reports a section as incomplete after its apparent fields
were supplied, re-read the application and the current generated schema
descriptions, then report which requirement remains unresolved. Do not infer a
hidden validation rule or repeatedly PATCH guessed values. The Developer API
contract and current state remain canonical.

## Follow-Ups

When progress reports follow-up work:

```bash
ramp applications followups --env production --agent
```

Inspect each follow-up's `actor`, `type`, `status`, `is_complete`,
`is_required`, `reason`, `prompt`, `description`, `applicant_action`,
`deep_link_url`, `accepted_document_types`, and `selected_document_type`.

- Skip follow-ups with `is_complete=true`.
- Treat `status=REVISION_REQUESTED` as a request to revise the existing response.
  Explain what changed using the current prompt and description; do not resend
  the prior response without checking it against the new request.
- If any follow-up has `actor=RAMP`, explain that Ramp owns the next step and
  stop.
- Otherwise, complete independently actionable `actor=AGENT` follow-ups before
  presenting applicant-owned follow-ups.
- Label incomplete follow-ups with `is_required=false` as optional. Do not let
  them delay submission when the API reports `is_submission_ready=true`.
- For `actor=APPLICANT`, explain the request and provide the current Ramp link
  after agent-owned work is complete. Do not call `update-followup` or `upload`
  for that follow-up.
- For an agent-owned document follow-up, `selected_document_type` selects a type;
  it does not upload a file. Prefer a listed `accepted_document_types` value
  when one matches. If the prompt or description requests a bespoke type, use
  that exact human-confirmed type even when it is not listed. Do not invent or
  generalize document types; Developer API validation remains canonical.
- If `selected_document_type` is already populated and matches the authorized
  file, do not update it again. Call `update-followup` only to set or change the
  selection.
- For sensitive or identity evidence, explain what will be transmitted and ask
  the applicant to select the document and explicitly authorize that exact file.
  Handle the decision case by case; prefer a direct applicant flow when the
  applicant does not want the file in agent context.

Text or agent-confirmable follow-up:

```bash
ramp applications update-followup <followup_id> --env production \
  --json '{"response_text":"<human-provided response>"}' --agent
```

Agent-owned document follow-up:

```bash
# Only when selected_document_type must be set or changed:
ramp applications update-followup <followup_id> --env production \
  --json '{"selected_document_type":"<confirmed type>"}' --agent
```

Upload the authorized file after confirming the current selection:

```bash
ramp applications upload --env production \
  --file <path> --purpose FOLLOWUP --followup_id <followup_id> --agent
ramp applications progress --env production --agent
```

For an agent-owned bank-linking follow-up, first inspect the current application
and linked accounts. If the applicant's Ramp checking preference is not clear,
ask whether they want to open a Ramp checking account. Separately ask whether
they want to link any external accounts; they may link multiple accounts through
Plaid or add multiple manual accounts. Add each manual account using only
applicant-confirmed details through `applications edit`. Re-fetch follow-ups and
progress after each account. Only after the applicant explicitly confirms that
there are no more accounts to link, send:

```bash
ramp applications update-followup <followup_id> --env production \
  --json '{"no_more_accounts_to_link":true}' --agent
```

Do not use this flag to bypass Plaid or another applicant-owned linking flow.

Fetch follow-ups again. Submit the follow-up package only when the response says
`is_submission_ready=true`:

```bash
ramp applications submit --env production --agent
ramp applications progress --env production --agent
```

Follow-up submission is not final application submission.

## Documents And Bank Accounts

List documents before deleting one so its ID and association are confirmed:

```bash
ramp applications documents --env production --page_size 100 --agent
ramp applications delete-document <document_id> --env production --agent
ramp applications progress --env production --agent
```

For bank documents, use the declared `purpose` and include the required
`manual_bank_account_id`. For follow-up documents, use `purpose=FOLLOWUP` and
the matching `followup_id`.

Paginate document and bank-account lists until `pagination.next_cursor` is null:

```bash
ramp applications accounts --env production --page_size 100 --agent
```

Agent output normalizes a next-page URL to the value accepted by these commands.
Pass each returned `pagination.next_cursor` through `--start` until it is null:

```bash
ramp applications documents --env production \
  --page_size 100 --start '<pagination.next_cursor>' --agent
ramp applications accounts --env production \
  --page_size 100 --start '<pagination.next_cursor>' --agent
```

Never fabricate or modify the returned cursor.

For every account whose `connection_provider` is `manual`, count the active
application documents whose `purpose` is `BANK_STATEMENT` and whose
`manual_bank_account_id` matches that account. The standard statement path is
complete once a manual account has three active bank statements. Three is the
completion threshold for this path, not an upload maximum.

Upload only the number of missing statements:

```bash
ramp applications upload --env production --file <statement_path> \
  --purpose BANK_STATEMENT --manual_bank_account_id <account_id> --agent
```

Repeat the list-and-count step after each upload and stop once the account has
three active statements. Then refresh application progress. Do not upload extra
statements merely because the API accepts more than three. If the applicant
is offered an alternative bank-document path in Ramp, hand off with the current
`deep_link_url`. The Developer API does not expose eligibility for that path, so
do not infer eligibility or select alternative documents on the applicant's
behalf.

Bank linking has multiple valid outcomes:

- Read the current application and inspect `open_ramp_checking_account`. If the
  applicant's preference is absent or unclear, ask whether they want to open a
  Ramp checking account before sending that field.
- Inspect `connection_provider` across the current linked accounts. An existing
  Ramp checking account may already satisfy the bank requirement.
- Ramp checking and external accounts are not mutually exclusive. The applicant
  may choose Ramp checking, one or more external accounts, or both.
- Reuse an existing applicant-confirmed manual account when one is present.
  Use `--connection_provider manual` only as a focused lookup after inspecting
  the current account set.
- Add each human-provided manual account through `applications edit`, re-reading
  accounts and progress after each addition.
- Plaid or another applicant-only linking flow must be handed to the human with
  the current `deep_link_url`. The applicant may repeat that flow to link
  multiple accounts when Ramp permits it.

Never ask for or store bank credentials.

Do not duplicate static validation rules in conversation. Use command help and
Developer API errors for the current field contract.
