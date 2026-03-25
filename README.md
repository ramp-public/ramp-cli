# ramp-cli

CLI for Ramp. Authenticate with OAuth, list and get resources, and manage expenses from your terminal or scripts.

## Install

```bash
curl -fsSL https://agents.ramp.com/install.sh | sh
```

Requires Python 3.11+.

## Quick Start

```bash
ramp auth login                              # OAuth via browser
ramp transactions list                       # List transactions (table)
ramp transactions list --agent | jq .        # JSON output for scripting
ramp transactions list --from-date 2025-01-01 --limit 100
ramp vendors create --name "Acme" --url "acme.com"
ramp business get                            # Business profile + balance
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
