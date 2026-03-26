# ramp-cli

CLI for Ramp's Developer API. Authenticate with OAuth, manage expenses, approve bills, book travel, and more — from your terminal or AI agent.

## Install

```bash
curl -fsSL https://agents.ramp.com/install.sh | sh
```

This detects your platform, downloads a pre-built binary, and sets up the `ramp` command.

**Alternative** (if you already have uv):

```bash
uv tool install git+https://github.com/ramp-public/ramp-cli.git
```

## Quick Start

```bash
ramp auth login                              # OAuth via browser
ramp users me                                # Current user details
ramp bills search --query "Acme"             # Search bills by vendor name
ramp transactions list --transactions_to_retrieve my_transactions
ramp transactions list --transactions_to_retrieve my_transactions --from_date 2025-01-01
ramp transactions list --agent               # JSON output for scripting
```

## Commands

| Command        | Description                                        |
| -------------- | -------------------------------------------------- |
| `auth`         | Login, logout, check status                        |
| `config`       | Get/set CLI configuration                          |
| `env`          | Show or set default environment (sandbox/production)|
| `applications` | Apply for a Ramp account                           |
| `skills`       | Browse and install agent skill instructions         |
| `feedback`     | Submit feedback about the CLI                      |

## Resources

11 resources, each with their own tools:

| Resource          | Tools                                                        |
| ----------------- | ------------------------------------------------------------ |
| `accounting`      | `categories`, `category-options`                             |
| `bills`           | `search`, `get`, `draft`, `pending`, `approve`, `attachments`|
| `funds`           | `list`, `activate`, `creds`, `lock`                          |
| `general`         | `comment`, `explain`, `help-center`, `policy`                |
| `purchase_orders` | `search`, `get`                                              |
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
