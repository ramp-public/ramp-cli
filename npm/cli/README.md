# @ramp/cli

CLI for Ramp's Developer API. Authenticate with OAuth, manage expenses, approve bills, book travel, and more — from your terminal or AI agent.

## Install

```bash
npm install -g @ramp/cli
```

Or run directly without a global install:

```bash
npx @ramp/cli --help
```

Requires Node.js 22+ and npm 10+ (Python is not required). Supported platforms: macOS (Apple Silicon & Intel), Linux (glibc x64 & arm64), and Windows (x64).

For one-command Ramp Router setup, see [`@ramp/router`](https://www.npmjs.com/package/@ramp/router) (`npx @ramp/router`).

## Quick Start

```bash
ramp auth login                              # OAuth via browser
ramp users me                                # Current user details
ramp bills search --query "Acme"             # Search bills by vendor name
ramp cards list                              # List your cards (card_state shows ACTIVE)
ramp transactions list --transactions_to_retrieve my_transactions
ramp transactions list --transactions_to_retrieve my_transactions --from_date 2025-01-01
ramp transactions list --transactions_to_retrieve my_transactions --rationale "Monthly review" --agent  # JSON output for scripting
ramp router                                  # Open the Router workspace
ramp update                                  # Update to the latest release
```

## Commands

| Command        | Description                                         |
| -------------- | --------------------------------------------------- |
| `auth`         | Login, logout, check status                         |
| `config`       | Get/set CLI configuration                           |
| `env`          | Show or set default environment (sandbox/production)|
| `profile`      | Show, list, or switch credential profiles           |
| `router`       | Connect coding agents, manage keys and strategies   |
| `applications` | Apply for a Ramp account                            |
| `skills`       | Browse and install agent skill instructions         |
| `update`       | Update the CLI to the latest release                |
| `feedback`     | Submit feedback about the CLI                       |

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

## License

MIT — see [https://github.com/ramp-public/ramp-cli](https://github.com/ramp-public/ramp-cli) for full documentation and source.
