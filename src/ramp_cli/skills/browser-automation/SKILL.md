---
name: browser-automation
description: "Automate a browser for web tasks — navigate sites, fill forms, click elements, take snapshots, and extract content. Powered by the browse CLI (Browserbase) with local headed Chrome or remote cloud sessions. Use when asked to interact with a website, fill out a checkout form, scrape content, or perform any browser-based workflow."
---

# Browser Automation

Automate a browser via the `browse` CLI (Browserbase). Runs headed Chrome locally for attended flows — the user can watch and intervene — and can run remote Browserbase cloud sessions for unattended work or bot-protected sites.

This skill covers setup and the payment-flow rules that matter for Ramp workflows. The full command reference is the `browse` skill bundled with the CLI itself — install it once and it stays in sync with the installed CLI version.

## Setup

```bash
npm install -g browse     # the CLI
browse skills install     # installs the canonical browse skill (full command reference) for your agent
browse doctor             # verify browser discovery and session prerequisites
```

Remote cloud sessions additionally need `export BROWSERBASE_API_KEY=...` — not required for local use.

No launcher script or profile directories are needed; `browse` manages its own background daemon. For command details beyond this file, use the installed `browse` skill or `browse <command> --help`.

## Quickstart

```bash
browse open https://example.com --local --headed
browse snapshot --filter /card|submit|checkout/i   # accessibility tree with element refs
browse fill @1-10 "text"
browse click @1-42
browse screenshot --path page.png
browse stop
```

Refs come from the snapshot (`@frame-element`, e.g. `@1-10`) and span iframe boundaries — fields inside payment iframes (Stripe, Braintree) get refs too. Flags go after the command (`browse open <url> -s checkout`). Re-snapshot after navigation or DOM changes; refs go stale.

## Always pass `--headed` for attended flows

Managed local sessions default to **headless**. Pass `--headed` on the command that opens the session so the user gets a visible Chrome window they can watch and intervene in — this matters most when money is moving.

**Always use `--headed` for:**

- Agent-card purchases or any payment flow
- Logged-in workflows where a session may need to be re-authenticated
- Any task where the user is at the keyboard and would benefit from seeing the browser

Headless is only appropriate for unattended scraping or background inspection where no human is watching, and even then ask the user first. A session keeps its mode for its lifetime — switching requires `browse stop` and re-opening.

## Session state is ephemeral

The managed local profile is temporary: cookies and logins survive navigation within a session but are **wiped on `browse stop`**. Don't stop a session mid-task if you'll need its login state. For durable authentication:

- `--auto-connect` attaches to the user's own running Chrome (launched with remote debugging), reusing their real logins and cookies
- Remote sessions persist state via Browserbase contexts (`browse cloud contexts create`, then `browse cloud sessions create --context-id <id> --persist`)

When a site requires login: prefer SSO/OAuth, ask the user for credentials (never guess), and finish the task before stopping the session.

## Bot checks, CAPTCHAs, and human handoff

When you hit a CAPTCHA, reCAPTCHA, 3DS challenge, login wall, or "verify you're human" page, do not retry programmatically. On payment flows the agent should not attempt a programmatic solve. Hand the wheel to the user:

1. **Confirm the user can see the page.** Local: the window must be headed — if you opened headless, `browse stop` and re-open with `--headed`. Remote: get the live view link with `browse cloud sessions debug <session-id>` and send the user the `debuggerFullscreenUrl`.
2. **Screenshot the current state:** `browse screenshot --path handoff.png`
3. **Hand off in plain language.** Example: "There's a reCAPTCHA on the checkout. Switch to the Chrome window I opened (or open the live view link), solve it, and reply when you're past it."
4. **Wait.** Do not poll the page. Wait for the user's reply.
5. **Resume.** Re-snapshot before clicking — refs may have changed during their interaction.

For non-payment workflows on bot-protected sites, `--remote` runs the session on Browserbase with stealth and proxy support — often avoiding the block entirely instead of handing it off.

## Debugging checkout issues

When a checkout form behaves unexpectedly, capture network traffic:

```bash
browse network on      # prints the capture path
# ... reproduce the issue ...
browse network off
```

Look for failing requests and HTTP status codes — they reveal what the UI may not show.

## Multi-step checkout wizards

Some checkout widgets use multi-step wizards where the payment iframe is destroyed between steps. Card data entered in step N may not be preserved when you reach step N+3.

1. Prefer merchants with single-page checkout forms — fewer points of failure
2. Look for a direct checkout URL instead of an embedded widget
3. If stuck with a multi-step wizard, move through the non-card steps as quickly as possible

## Best practices

- Don't stop to narrate intermediate states — keep clicking through auth flows and loading screens until you hit an actual blocker
- Try the action before assuming it won't work
- Use `browse wait load` / `browse wait selector "#result"` instead of sleeping for page loads and async content
- `fill` sets values programmatically; if a field rejects it (some React forms, tokenized card inputs), `browse click <ref>` to focus, then `browse type "<text>"` for real keystrokes
- `<select>` elements need `browse select <ref> "Option Label"`, not `fill`
- `browse eval` takes a plain expression (`browse eval 'document.title'`) — arrow functions silently return an empty result
- Run `date` at session start to know current date/time for interpreting relative dates
