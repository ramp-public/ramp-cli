# ramp-cli

CLI for Ramp's Developer API. Authenticate with OAuth, manage expenses, approve bills, book travel, and more — from your terminal or AI agent.

## Install

```bash
curl -fsSL https://agents.ramp.com/install.sh | sh
```

This detects your platform, downloads a pre-built binary, and sets up the `ramp` command.

After installation, open the Router workspace to connect coding agents and
desktop apps, manage API keys, edit routing profiles, and choose subagent
models:

```bash
ramp router                          # or: ramp router ui
ramp router ui --inline --height 18  # beneath your prompt (macOS/Linux)
ramp router ui --theme light         # dark, light, terminal, or auto (default)
```

Run `npx @ramp/router@latest configure --enable-agent` to sign in, choose harnesses, and enable the Router agent.
`RAMP_ROUTER_THEME` sets a default theme and `RAMP_ROUTER_REDUCED_MOTION=1`
turns off animation. Interactive `router configure`, `keys`, `strategies`,
`account`, and `subagents` open their workspace screens.

Scripts never get the UI: agent mode, `--no-input`, JSON output, `--quiet`,
redirected stdin/stdout, and `TERM=dumb` keep every command's existing
noninteractive behavior, and `router ui` in those modes is a usage error:

```bash
ramp --agent router keys list
ramp --agent --no-input router configure codex  # key from RAMP_ROUTER_CONFIGURE_API_KEY or --setup-file
```

To start directly at harness setup:

```bash
ramp router configure
```

New production setups use `https://api.router.com/v1` for Router requests.
`ramp router refresh` migrates the exact previous production endpoint
(`https://router-api.ramp.com/v1`) in CLI-managed agent setups to the canonical
host. For Claude Code, the configured `ANTHROPIC_BASE_URL` is the host without
`/v1`. Other saved deployments and current `RAMP_ROUTER_BASE_URL` /
`LLM_GATEWAY_BASE_URL` overrides are preserved. Claude Desktop's Router profile
migrates on refresh when Claude Desktop is closed. Refresh does not interrupt
a running Desktop session; to migrate immediately, run
`ramp router configure desktop --base-url https://api.router.com/v1`.

Without a terminal (for example, during an MDM install), omitting client names
configures all automatic integrations, including Claude Desktop/Cowork when
installed on macOS. Claude Desktop restarts to apply its Router profile.
All integrations use the same key acquired during that run.
Pass client names, such as `ramp router configure codex`, to limit setup.
For unattended redeployments, add `--reuse-existing-key` to reuse a saved key
for the selected Router deployment without opening the browser. If no compatible
key is saved, setup still requests browser approval. If multiple saved keys
disagree, setup stops so the user can choose a key interactively rather than
silently replacing their credentials. An invalid saved key also fails setup;
run `ramp router configure` without this option to create or select a replacement.

To connect to another Router deployment, pass `--base-url` (the gateway `/v1`
URL written into agent configs) and, for a deployment the CLI does not already
know, `--ui-url` (the web app origin used for browser setup and dashboard
links), or set `RAMP_ROUTER_BASE_URL` and `RAMP_ROUTER_UI_URL`. The installer forwards the same flags when given
`--router --base-url <URL> --ui-url <URL>`:

```bash
ramp router configure --base-url https://internal-api.router.com/v1 --ui-url https://internal.router.com
```

**Homebrew** (macOS and Linux):

```bash
brew install ramp-public/ramp/ramp-cli
```

**Alternative** (if you already have uv):

```bash
uv tool install git+https://github.com/ramp-public/ramp-cli.git
```

## Codex model picker

Codex CLI and Desktop use Router's recommended model shortlist by default.
Connect normally, or explicitly select the compact view:

```bash
ramp router configure codex --codex-models compact --reuse-existing-key
```

This uses the same recommendations as Claude Code's compact view, with Codex's
own model capabilities and reasoning settings. Your current model is preserved.
Other catalog entries remain hidden where space permits so existing chats can
still resolve their model metadata. Refreshes and model changes preserve the
saved view. Existing connections without a saved preference switch to compact
on their next configure or refresh. An explicit `all` preference stays unchanged.

To restore the full model picker:

```bash
ramp router configure codex --codex-models all --reuse-existing-key
```

Use the same `--base-url` as your existing connection if it is on another Router
deployment. On macOS, configure quits and reopens running Codex and ChatGPT desktop
apps after saving the settings. Restart any running Codex CLI sessions manually.
Background refreshes leave running apps alone; their updated catalogs apply when
Codex next reloads them.

## Router agent: background refresh and repository tagging

One optional agent keeps configured Router model lists and defaults current and
tags supported local coding-agent requests with their Git repository. It is
**disabled by default**:
`ramp update`, configure, and refresh do not opt you in.

After configuring Router on macOS or Linux:

```bash
ramp router agent enable
ramp router agent status
ramp router agent disable
```

`enable` starts the native agent's local listener, checks its health, and refreshes
existing Claude Code, Codex, OpenCode, and Pi setups to use it. The same process
handles background refresh; there is no separate repository-helper command.
Use `ramp router agent enable --port 47999` to select a different local port.
Restart open coding-agent sessions after enabling or disabling so they reload
their saved endpoints. Cursor's cloud-hosted requests are not tagged.

Clients use a per-user local token for requests to the agent. The agent checks
the caller's OS user, then replaces that token with the Router key stored in a
private file. Each token is limited to one Router host. Direct account and usage
calls continue to use the Router key. After an upgrade from an older tagging
preview, run `ramp router agent enable` and restart open coding-agent sessions
to replace their saved credentials and endpoints.
In Pi, a new key entered through `/login` uses the direct Router endpoint until
`ramp router configure pi` pairs it with the agent again. A direct endpoint
override for the same deployment uses the saved Router key, not the local token.
An override for a different deployment needs that deployment's key.

The choice is saved per user and survives shell restarts, logins, CLI updates, and
refreshes. Enabling installs a launchd or systemd user service that starts at login
and restarts after a crash. `ramp router refresh` reapplies the opted-in setup but
never opts an unenabled or disabled user in. Disabling stops background refresh
and tagging and restores direct Router endpoints, without disconnecting Router
or changing session-start hooks. If a client cannot be restored, an untagged
passthrough listener stays until repair succeeds; run refresh and disable again.
`status` reports refresh, tagging, listener health, and service state;
`--agent` returns one JSON envelope with tagging diagnostics under
`repository_tagging`.
If a client refresh fails during enable, the background preference remains on.
The command and `status` report the failed clients under `refresh_failures` in
the tagging diagnostics. Repair those clients, then run enable again.

Binary releases include the Rust agent, which checks about every five minutes.
Installs without it use a 15-minute scheduled job when enabled, without repository
tagging. Existing sync-only opt-ins remain unchanged until `agent enable` is run
again. Linux needs a
working systemd user session. On macOS, a disabled Login Items entry must also be
allowed in System Settings. `RAMP_NO_SESSION_SYNC` still blocks automatic sync;
unset it before explicitly enabling the background service.

Session-hook and scheduled refreshes also repair missing CLI-owned OpenCode/Pi
plugin paths after an upgrade moves the bundled plugins, even when the OS service
is disabled and only session hooks are in use. They preserve plugin options,
credentials, custom registrations, and config comments. An existing plugin path
is left alone; use `ramp router refresh` for full setup repair.

## Router connectors MCP

Set up Router login, connector tools, and your coding harnesses in one command:

```bash
ramp router configure --connectors
```

Choose from Claude Code, Codex, and OpenCode, or name them explicitly, for example
`ramp router configure codex --connectors`. Setup signs in with connector consent
on every run, enables connectors for the selected harnesses, and configures them
with a Router API key. If no compatible key is saved locally, it creates a
`Ramp CLI` key through the Router key API and uses it immediately, without a reuse
prompt. If a saved key exists, setup asks whether to reuse it or create a new one.
Use `--reuse-existing-key` to reuse a saved key without the key-selection prompt;
OAuth sign-in still runs. `--no-input` and `--agent` are incompatible with this
flow. Use `--no-browser` to print the sign-in URL and open it yourself.
Previously enabled connector registrations for the same Router account,
business, deployment, and credential profile are renewed for the new sign-in
too, even when their harness is not selected. Their provider settings are left
unchanged; disabled, custom, and other-account registrations are not retargeted.

To authorize and enable connectors separately:

```bash
ramp router login --connectors
ramp router connectors status --output json
ramp router connectors enable --harness claude-code
```

The supported harness IDs are `claude-code`, `codex`, and `opencode`. Enable each
selected harness explicitly; this does not configure Claude Desktop or change
existing Slack, Google, or other provider connections. To remove only the CLI's
connector registration, run `ramp router connectors disable --harness claude-code`.
Unrelated harness settings and MCP servers are preserved; foreign or customized
`ramp-connectors` entries are not overwritten.

Connector setup uses **Router account authentication**, not `ramp auth login`
(Developer API authentication). Supported deployments are the public, internal,
QA, and internal QA Router origins. Capability discovery is available through
`ramp router connectors capabilities --output json` (`router-connectors-mcp-v1`).

Harness configuration contains only the CLI executable and an opaque binding ID,
never tokens. Each registration pins the Router deployment, MCP resource, profile,
user, business, Core identity, and authorizing device session. Signing in again,
even as the same user, requires explicit re-enable; status reports the old
registration as `conflict`. Status is local/cached and does not guarantee live
server authorization. The bridge never opens a browser to recover authentication.

Credentials remain in the private Router CLI store. Processes share its locked,
durable refresh protocol and a private short-lived MCP token cache. Logging out or
revoking the authorizing session prevents further issuance; an already-issued MCP
token can remain valid for at most 300 seconds after issuance plus the existing
10-second server clock tolerance (310 seconds total), not immediate revocation.
Other sessions and provider connections are unaffected.

The local stdio bridge forwards newline-delimited JSON-RPC over HTTPS POST without
following redirects. It accepts JSON or SSE replies; the current Router gateway
is stateless and does not provide standalone GET/server-push sessions.

## Manage Router API keys

Run `ramp router login`, then `ramp router keys` to select a key and inspect
its usage. The selected-key menu lets you rename it, choose an existing routing
profile, or lock/unlock it. Locking stops requests without deleting the key;
unlocking remains subject to account and administrator restrictions.
Changing profiles moves only that key, without editing the shared profile.
Profile selection requires routing profiles to be available for your account.

In `ramp router ui`, the **Routing Profiles** tab starts new profiles from the
dashboard's workload presets: General Purpose Agents, Low latency, Automations,
and Research & analysis. Profiles have
an immutable routing slug. Select a profile's name → **Edit strategies** to edit
the controls supported by its preset, and attach keys using the **API keys** cell. Removing
a key returns it to the account's default profile. You can also assign a profile
from the **Routing Profile** cell in **API Keys**, or choose one
when creating a key. Harnesses sharing a key use the same profile; admin-issued
keys keep the profile selected by their administrator.
The **Harnesses** tab holds **Global Coding Settings**: cost-efficient routing
and cross-provider preference, shared across recognized coding-agent traffic for
the signed-in Router account. **Experimental models → Configure** adds
`switchyard` and `auto-jev-routing` to the model picker alongside your usual models.
Switchyard requires both an efficient and a frontier model; sensitivity is under
**Advanced**. Auto requires 2–6 distinct models, each with a task description;
the first is the default. Untouched inherited settings are preserved when saving.
Ordinary and Experimental coding settings are separate, one-per-account profiles.
Neither accepts API-key assignments. Historical assignments remain visible in
Routing Profiles and can be moved out from API Keys. Model selection stays per
harness; saving Experimental settings does not switch an existing session's model.
Research & analysis lets you choose benchmarks and weights totaling 100%, or
turn Benchmark routing off. Saving creates or updates the benchmark alias named
after the profile slug; select that slug in your harness model picker to use it.
Benchmark edits are saved before the profile, so a partial-save message means
those edits may already affect requests. Retry to finish; leaving does not undo
them. Unrelated edits preserve older alias bindings and their stored weight ratios.

Existing profiles retain their previously enabled strategies for editing, even
when their preset no longer offers those strategies for new profiles. Stored
Switchyard pairs show **Model pair**; turning Switchyard off clears the pair.
Unrelated edits preserve inherited and advanced settings. Slugless profiles are
**Legacy**: create a preset profile and move their keys there to change strategies.
The Account default profile cannot be renamed or deleted.

For scripts and AI agents:

```bash
ramp router keys rename KEY_ID --name "Coding"
ramp router keys set-strategy KEY_ID --routing-profile "Cheap"
ramp router keys lock KEY_ID
ramp router keys unlock KEY_ID
```

All four commands support `--dry-run` and the standard `--agent` JSON output.
These changes do not rotate the key's secret or rewrite agent configurations.

For routing profiles, `ramp router strategies` opens the account editor in
human-readable mode. Machine-readable listings preserve the configured API key's
strategy-settings response, even after `ramp router login`. Select account profiles
explicitly with `ramp --agent router strategies list --account` (or
`ramp --agent router strategies --account`). `--account` and `--api-key` cannot be
combined. Account profiles are changed with `strategies create/edit/delete`;
`strategies enable/disable` continues to change key-owner settings.

Strategy saves apply settings and key assignments in separate requests. If a save
partially fails, some changes may already be live. The editor reloads the saved
state and preserves your remaining edits for review; if it cannot reload, editing
stops until you reopen the strategy. Browser reauthorization that changes the
user or business stops a pending write so you can review the account and rerun it.

## Quick Start

```bash
ramp auth login                              # OAuth via browser
ramp users me                                # Current user details
ramp bills search --query "Acme"             # Search bills by vendor name
ramp cards list                              # List your cards (card_state shows ACTIVE)
ramp cards list --rationale "Count active cards" --agent | jq '[.data[0].cards[] | select((.card_state // "" | ascii_downcase) == "active")] | length'  # Count active cards
ramp transactions list --transactions_to_retrieve my_transactions
ramp transactions list --transactions_to_retrieve my_transactions --from_date 2025-01-01
ramp transactions list --transactions_to_retrieve my_transactions --rationale "Monthly review" --agent  # JSON output for scripting
```

### Standalone agent authentication

Standalone agents authenticate without a browser by exchanging OAuth client
credentials for a production access token with a server-reported lifetime. Configure `RAMP_CLIENT_SECRET`
in your agent runtime or CI secret store instead of exporting a literal secret from an
interactive shell. For example, in GitHub Actions:

```yaml
env:
  RAMP_CLIENT_ID: ${{ vars.RAMP_CLIENT_ID }}
  RAMP_CLIENT_SECRET: ${{ secrets.RAMP_CLIENT_SECRET }}
steps:
  - run: ramp --env production agent login
```

`RAMP_CLIENT_ID` and `RAMP_CLIENT_SECRET` are read automatically. Client-credentials
tokens cannot be refreshed. Use the `expires_in` value from the login output to
schedule the next full login before the access token expires. Human users should use
`ramp auth login` and the browser-based PKCE flow instead.

### Switching identities with profiles

Store human and agent credentials separately, then switch the active identity without
logging in again:

```bash
ramp auth login
ramp agent login
ramp profile human
ramp profile agent
ramp profile list
ramp --profile human funds list --rationale "Review my human-owned funds"
```

The CLI stores these identities separately as `human` and `agent`. The active
profile is used by subsequent commands. Use `--profile` for a single command
without changing the default. `RAMP_PROFILE` pins an identity for automation and
cannot be overridden by a conflicting `--profile` flag.

## Commands

| Command        | Description                                        |
| -------------- | -------------------------------------------------- |
| `auth`         | Login, logout, check status                        |
| `config`       | Get/set CLI configuration                          |
| `env`          | Show or set default environment (sandbox/production)|
| `profile`      | Show, list, or switch credential profiles           |
| `applications` | Apply for a Ramp account                           |
| `skills`       | Browse and install agent skill instructions         |
| `feedback`     | Submit feedback about the CLI                      |

## Resources

12 resources, each with their own tools:

| Resource          | Tools                                                        |
| ----------------- | ------------------------------------------------------------ |
| `accounting`      | `categories`, `category-options`                             |
| `bills`           | `search`, `get`, `draft`, `pending`, `approve`, `attachments`|
| `cards`           | `list`, `activate`, `lock` (aliases also under `funds`)      |
| `funds`           | `list`, `activate`, `creds`, `lock`                          |
| `general`         | `comment`, `explain`, `help-center`                          |
| `purchase-orders` | `search`, `get`                                              |
| `receipts`        | `upload`, `attach`                                           |
| `reimbursements`  | `list`, `pending`, `submit`, `approve`, `edit`               |
| `requests`        | `pending`, `approve`                                         |
| `transactions`    | `list`, `get`, `approve`, `edit`, `missing`, `flag-missing`, `explain-missing`, `memo-suggestions`, `trips` |
| `travel`          | `list`, `create`, `bookings`, `locations`                    |
| `users`           | `list`, `me`, `org-chart`                                    |

Usage: `ramp <resource> <tool> [OPTIONS]`

## Global Flags

| Flag               | Description                                       |
| ------------------ | ------------------------------------------------- |
| `--env`, `-e`      | `production` (default) or `sandbox`               |
| `--output`, `-o`   | Output format: `json` or `table`                  |
| `--agent`          | Machine-readable JSON output (default when piped) |
| `--human`          | Human-readable table output (default in terminal) |
| `--wide`           | Show all columns in table output                  |
| `--quiet`, `-q`    | Suppress progress output                          |
| `--no-input`       | Disable interactive prompts (for CI/scripts)      |

## Tool Flags

Each tool has its own flags. Common patterns:

| Flag                   | Description                              |
| ---------------------- | ---------------------------------------- |
| `--json TEXT`          | Raw JSON request body (bypasses flags)   |
| `--dry_run`, `-n`      | Print request without sending            |
| `--page_size N`        | Results per page                         |
| `--next_page_cursor`   | Resume pagination from previous response |

Run `ramp <resource> <tool> --help` to see all available flags for a tool.

## Agent Mode

`--agent` outputs JSON for scripting and AI agent consumption. Pipe to `jq` for processing:

```bash
ramp transactions list --transactions_to_retrieve my_transactions --rationale "Monthly review" --agent | jq '.data[0]'
ramp users me --rationale "Look up my user ID" --agent | jq '.data[0].user_id'
```

## Development

```bash
git clone https://github.com/ramp-public/ramp-cli.git
cd ramp-cli
uv sync
uv run pre-commit install --install-hooks
uv run pre-commit run --all-files
uv run pytest tests/ -v
```

## License

See [LICENSE](LICENSE) for details.
