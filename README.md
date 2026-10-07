# ramp-cli

CLI for Ramp's Developer API. Authenticate with OAuth, manage expenses, approve bills, book travel, and more — from your terminal or AI agent.

## Install

```bash
curl -fsSL https://agents.ramp.com/install.sh | sh
```

This detects your platform, downloads a pre-built binary, and sets up the `ramp` command.

After installation, open the Router workspace to connect coding agents and
desktop apps, manage API keys, edit routing strategies, and choose subagent
models:

```bash
ramp router                          # or: ramp router ui
ramp router ui --inline --height 18  # beneath your prompt (macOS/Linux)
ramp router ui --theme light         # dark, light, terminal, or auto (default)
```

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

## Manage Router API keys

Run `ramp router login`, then `ramp router keys` to select a key and inspect
its usage. The selected-key menu lets you rename it, choose an existing routing
strategy, or lock/unlock it. Locking stops requests without deleting the key;
unlocking remains subject to account and administrator restrictions.
Changing strategy moves only that key, without editing the shared strategy.
Strategy selection requires routing profiles to be available for your account.

For scripts and AI agents:

```bash
ramp router keys rename KEY_ID --name "Coding"
ramp router keys set-strategy KEY_ID --routing-profile "Cheap"
ramp router keys lock KEY_ID
ramp router keys unlock KEY_ID
```

All four commands support `--dry-run` and the standard `--agent` JSON output.
These changes do not rotate the key's secret or rewrite agent configurations.

For routing strategies, `ramp router strategies` opens the account editor in
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
ramp cards list --agent | jq '[.data[0].cards[] | select((.card_state // "" | ascii_downcase) == "active")] | length'  # Count active cards
ramp transactions list --transactions_to_retrieve my_transactions
ramp transactions list --transactions_to_retrieve my_transactions --from_date 2025-01-01
ramp transactions list --agent               # JSON output for scripting
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
| `general`         | `comment`, `explain`, `help-center`, `policy`                |
| `purchase-orders` | `search`, `get`                                              |
| `receipts`        | `upload`, `attach`                                           |
| `reimbursements`  | `list`, `pending`, `submit`, `approve`, `edit`               |
| `requests`        | `pending`, `approve`                                         |
| `transactions`    | `list`, `get`, `approve`, `edit`, `missing`, `flag-missing`, `explain-missing`, `memo-suggestions`, `trips` |
| `travel`          | `list`, `create`, `bookings`, `locations`                    |
| `users`           | `me`, `search`, `org-chart`                                  |

Usage: `ramp <resource> <tool> [OPTIONS]`

## Global Flags

| Flag               | Description                                       |
| ------------------ | ------------------------------------------------- |
| `--env`, `-e`      | `sandbox` (default) or `production`               |
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
ramp transactions list --transactions_to_retrieve my_transactions --agent | jq '.data[0]'
ramp users me --agent | jq '.data.user_id'
```


## Multi-business users

The CLI stores **one OAuth token per environment** (`sandbox` or `production`).
That token is bound to **one active business** at a time — the business you
select during `ramp auth login` in the browser.

If you belong to multiple Ramp businesses (for example, as a partner admin
across client accounts):

1. **See all memberships** — `ramp auth businesses` or `ramp users me --agent`
2. **Switch businesses** — run `ramp auth login` again and pick the target
   business in the OAuth UI (this replaces the stored token for that environment)
3. **Confirm active session** — `ramp auth businesses` marks which `business_id`
   matches your current token; `ramp auth status` shows authentication state

There is no `ramp auth switch` command yet — business selection happens in the
OAuth flow. See [issue #1](https://github.com/ramp-public/ramp-cli/issues/1)
for discussion.

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
