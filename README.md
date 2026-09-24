# ramp-cli

CLI for Ramp's Developer API. Authenticate with OAuth, manage expenses, approve bills, book travel, and more — from your terminal or AI agent.

## Install

```bash
curl -fsSL https://agents.ramp.com/install.sh | sh
```

This detects your platform, downloads a pre-built binary, and sets up the `ramp` command.

After installation, choose which coding agents and desktop apps to connect to
Ramp Router. The picker lists the integrations found on your machine:

```bash
ramp router configure
```

New production setups use `https://api.router.com/v1` for Router requests.
`ramp router refresh` migrates the exact previous production endpoint
(`https://router-api.ramp.com/v1`) in CLI-managed agent setups to the canonical
host. For Claude Code, the configured `ANTHROPIC_BASE_URL` is the host without
`/v1`. Other saved deployments and current `RAMP_ROUTER_BASE_URL` /
`LLM_GATEWAY_BASE_URL` overrides are preserved. Claude Cowork's Desktop profile
migrates on refresh when Claude Desktop is closed. Refresh does not interrupt
a running Desktop session; to migrate immediately, run
`ramp router configure cowork --base-url https://api.router.com/v1`.

Without a terminal (for example, during an MDM install), omitting client names
configures all automatic integrations, including Claude Desktop/Cowork when
installed on macOS. Claude Desktop restarts to apply its Router profile.
All integrations use the same key acquired during that run.
Pass client names, such as `ramp router configure codex`, to limit setup.

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
