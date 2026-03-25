---
name: browser-automation
description: "Automate Google Chrome for web tasks — navigate sites, fill forms, click elements, take snapshots, and extract content. Powered by playwright-cli (pw) with a persistent browser profile. Use when asked to interact with a website, fill out a checkout form, scrape content, or perform any browser-based workflow."
---

# Browser Automation

Automate Google Chrome via `playwright-cli` (pw). Maintains a persistent Chrome profile at `~/.pw-agent/.playwright-profile/` so logins and cookies survive across sessions.

## Prerequisites

- Node.js installed
- `playwright-cli` installed: `npm install -g @playwright/cli`
- Minimum version: `0.1.0` (check with `playwright-cli --version`)
- If `playwright-cli` is not found after install, the npm global bin may not be in PATH. Fix with:
  ```bash
  export PATH="$(npm root -g)/../bin:$PATH"
  ```

## Setup

`~/.pw-agent/` is the runtime home. It holds the `pw` launcher script and persistent Chrome state.

### First-time setup

1. Create runtime directories:
   ```bash
   mkdir -p ~/.pw-agent/.playwright-profile ~/.pw-agent/.playwright-cli ~/.pw-agent/.playwright-mcp
   ```

2. Create a launcher script at `~/.pw-agent/pw`:
   ```bash
   cat > ~/.pw-agent/pw << 'LAUNCHER'
   #!/usr/bin/env bash
   cd "$(dirname "$0")"
   # Ensure npm global bin is in PATH
   NPM_BIN="$(npm root -g 2>/dev/null)/../bin"
   [ -d "$NPM_BIN" ] && export PATH="$NPM_BIN:$PATH"
   exec playwright-cli "$@"
   LAUNCHER
   chmod +x ~/.pw-agent/pw
   ```

3. Seed the profile by opening a site:
   ```bash
   cd ~/.pw-agent && ./pw open https://example.com
   ```
   Log in to any services you need. Close the browser when done. The profile persists for future sessions.

**CRITICAL: Never delete `~/.pw-agent/.playwright-profile/`** — it contains saved logins and cookies. Re-setup should only recreate the launcher script and `mkdir -p` missing directories.

## Usage

All commands run from `~/.pw-agent/`:

```bash
cd ~/.pw-agent && ./pw open https://example.com
./pw snapshot        # Capture full DOM as YAML accessibility tree
./pw screenshot      # Capture viewport as PNG
./pw click e42       # Click element by ref ID
./pw fill e42 "text" # Fill form field
./pw press Enter     # Press keyboard key
./pw stop            # Close browser
```

### Headless mode

On macOS, always ask the user before using `--headless` — users typically want to watch the browser.

```bash
./pw --headless open https://example.com   # Launch headless
./pw snapshot                               # Already running — no flag needed
```

Pass `--headless` only on the command that launches the browser (`open` for session 0, `start` for numbered sessions). Once launched, the session retains its mode.

### Multi-session support

Run parallel browser sessions with isolated contexts:

```bash
./pw 1 start https://site-a.com
./pw 1 snapshot
./pw 1 stop

./pw 2 start https://site-b.com   # Parallel session
./pw 2 snapshot
./pw 2 stop
```

Named sessions for readability:

```bash
./pw --session "checkout" open https://store.com
./pw --session "checkout" snapshot
./pw --session "checkout" click e42
./pw --session "checkout" stop
```

If `./pw N start` says "Session already active", pick another number or stop the existing one.

**Navigating within a session:** Don't use `open` on an already-running session. Use `eval` instead:

```bash
./pw eval "() => { window.location.href = 'https://example.com/new-page' }"
sleep 3  # Wait for SPA to render
./pw screenshot
```

### Output artifacts

- Snapshots: `.playwright-cli/page-<timestamp>.yml`
- Screenshots: `.playwright-cli/page-<timestamp>.png`
- Downloads: `.playwright-mcp/`

## Screenshots vs Snapshots

- **Screenshots** capture the visible viewport only
  - Use to verify visual state, debug layout, understand what's visible/hidden
  - Use to decide what action to take next (click, scroll, type)
- **Snapshots** capture the entire DOM, not just the visible viewport
  - Use to find element refs (`[ref=eNNN]`) for programmatic interaction
  - Use for greppable text content and DOM structure
  - Grep snapshots instead of reading hundreds of lines:
    ```bash
    grep -i "submit\|checkout\|button" .playwright-cli/page-*.yml | head -10
    ```

## Element refs

`./pw snapshot` produces a YAML accessibility tree with `[ref=eNNN]` identifiers for interactive elements:

```bash
./pw snapshot           # Find element refs
./pw fill e42 "query"   # Interact by ref
./pw click e15          # Click by ref
```

Refs are invalidated after page changes (navigation, AJAX, dynamic content) — re-snapshot to get fresh refs.

**Prefer keyboard over clicks** for more reliable interaction:
- `Tab` — next element / form field
- `Enter` — select autocomplete result, activate button
- `Meta+Enter` — submit forms, send messages
- `Escape` — close dialogs, cancel operations
- `PageUp`/`PageDown` — scroll to load more content

Wait for async content:

```bash
SNAPSHOT=$(./pw snapshot 2>&1 | grep -oE '\.playwright-cli/[^ )]+\.yml')
grep -iE "results\|loaded" "$SNAPSHOT" -C 5 || echo "not ready yet"
```

## Dropdown `<select>` elements

`fill` does NOT work on `<select>` elements. Use the `select` command instead:

```bash
./pw select <ref> "Option Label"   # Select by visible text
./pw select <ref> "value"          # Select by value attribute
```

The snapshot shows `<select>` elements as `combobox` roles. If `fill` errors with "Element is not an `<input>`", switch to `select`.

## Typing vs filling

`fill` sets the value programmatically — fast but may not trigger React/JS change handlers on some sites. If `fill` doesn't work:

1. Click the field first: `./pw click <ref>`
2. Clear it: `./pw press Meta+a` then `./pw press Backspace`
3. Type character by character: `./pw press 3` then `./pw press .` then `./pw press 4` etc.

Some numeric inputs strip non-numeric characters (e.g., `.` in donation amount fields that only accept whole numbers).

## Nested iframes

Payment forms (Stripe, Braintree) are often inside nested iframes. Playwright-cli handles this transparently — element refs like `f20e21` (with `f` prefix + nested frame IDs) work across iframe boundaries. Just use the ref from the snapshot as you would any other element.

Stripe iframe refs change when the iframe is recreated (e.g., navigating between wizard steps). Always re-snapshot to get fresh refs after page transitions.

## Dismissing popups and modals

Many sites show cookie banners, login prompts, or promotional modals:

```bash
SNAPSHOT=$(./pw snapshot 2>&1 | grep -oE '\.playwright-cli/[^ )]+\.yml')
grep -iE "dismiss|close|no.thanks|decline|accept.*cookie" "$SNAPSHOT" | head -5
./pw click e42   # Click the dismiss button
```

## JavaScript evaluation

Use heredocs to avoid shell quoting issues:

```bash
./pw eval "$(cat <<'EOF'
() => {
  var links = document.querySelectorAll('a[href]');
  return Array.from(links).map(a => a.href).slice(0, 10);
}
EOF
)"
```

**IMPORTANT:** `eval` expects a function definition, not a value expression:

```bash
# DO THIS — pass a function
./pw eval "() => document.title"

# NOT THIS — causes "TypeError: result is not a function"
./pw eval "document.title"
```

## Scrolling

`mousewheel` requires positioning the mouse over the scrollable content first:

```bash
./pw mousemove 600 400   # Position over content
./pw mousewheel 500 0    # Scroll down 500px
```

- `mousewheel 500 0` = scroll down 500px
- `mousewheel -500 0` = scroll up 500px
- `mousewheel 0 500` = scroll right 500px

## Authentication tips

When a site requires login:

1. Check if there's an SSO/OAuth option — prefer it over username/password
2. If you need credentials, ask the user — never guess passwords
3. For sites with saved sessions, the persistent profile may already be authenticated
4. After logging in, the session persists in the profile for future use

## Debugging checkout issues

When a checkout form behaves unexpectedly, check browser console logs:

```bash
cat ~/.pw-agent/.playwright-cli/console-*.log | tail -30
```

Look for HTTP status codes and error events — they reveal what the UI may not show.

## Multi-step checkout wizards

Some checkout widgets use multi-step wizards where the Stripe iframe is destroyed between steps. Card data entered in step N may not be preserved when you reach step N+3.

**Workaround strategies:**
1. Prefer merchants with single-page checkout forms — fewer points of failure
2. Look for a direct checkout URL instead of an embedded widget
3. If stuck with a multi-step wizard, move through the non-card steps as quickly as possible

## Best practices

- Don't stop to narrate intermediate states — keep clicking through auth flows and loading screens until you hit an actual blocker
- Try the action before assuming it won't work
- Busywait with exponential backoff for page loads and async content
- After a browser session, summarize actions in shorthand:
  `open <url>` -> `click e42` -> `fill e15 "query"` -> `press Enter`
- Run `date` at session start to know current date/time for interpreting relative dates
