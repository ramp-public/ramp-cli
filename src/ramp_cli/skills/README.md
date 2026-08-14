# Ramp CLI Skills

[Agent skills](https://agentskills.io) that teach coding agents (Claude Code, Cursor, Codex, Gemini CLI, etc.) how to take actions through the `ramp` CLI on behalf of a user.

## Skills

| Skill | What it does |
|-------|-------------|
| `get-started` | One-fetch entry point — detects new vs existing customer and routes onboarding across Claude Desktop, ChatGPT, Claude Code, Codex, and Perplexity |
| `agentic-purchase` | End-to-end agent card purchasing via browser checkout |
| `apply-to-ramp` | Start a new or continue an existing Ramp financing application |
| `book-flight` | Search, compare, and book one-way or round-trip flights conversationally |
| `book-hotel` | Search, compare, preview, and book hotel rooms conversationally |
| `card-management` | Inspect card/fund status, count active contexts, and activate or lock cards |
| `approval-dashboard` | Review and approve pending transactions, bills, reimbursements, and requests |
| `browser-automation` | Automate Google Chrome for web tasks — navigate, fill forms, extract content |
| `incorporate-with-ramp` | File a US LLC through Ramp when a Ramp application needs incorporation |
| `manage-bills` | Search, view, and manage bills with deep link handoff to the Ramp web app |
| `manage-procurement` | Find and track submitted procurement requests and purchase orders; handle procurement-specific approvals |
| `payment-lookup` | Look up vendor bill payments and verify payment status |
| `receipt-compliance` | Find transactions missing receipts and upload them |
| `spend-analysis` | Analyze spend by vendor, category, or team over a date range |
| `submit-procurement-request` | Create, fill, continue, review, and submit draft procurement requests |
| `submit-reimbursement` | Submit an out-of-pocket reimbursement from a receipt |
| `transaction-cleanup` | Complete missing items on transactions — memos, accounting categories, funds |
| `vendor-document-upload` | Upload vendor tax, legal, and payment documents and review bulk matching status |
| `x402-pay` | Pay an x402 (HTTP 402) payment challenge with the business's agentic Solana USDC wallet |

## Install

### Via `ramp` CLI (recommended)

The CLI downloads the latest verified catalog from
[`ramp-public/skills`](https://github.com/ramp-public/skills) and installs each
skill with a `ramp-` prefix (for example, `browser-automation` is installed as
`ramp-browser-automation`) so its ownership is clear alongside skills from
other providers. The last verified catalog remains available locally until a
new release is downloaded.

```bash
# Install one skill into the detected agent directory
ramp skills install browser-automation

# Install all skills
ramp skills install --all

# Install to a specific directory
ramp skills install --all --target ~/.cursor/skills
```

Reinstalling a Ramp-managed `ramp-*` directory refreshes its CLI-owned
`SKILL.md`; other files remain until uninstall removes the managed directory.
To customize a skill, copy it under a different name; the CLI never writes into
an unreceipted directory.

## Remote release contract

[`ramp-public/skills`](https://github.com/ramp-public/skills) is the source of
truth for new skill content. Its independent GitHub Releases must publish these
two assets:

- `ramp-skills.tar.gz`, containing `skills/ramp-<name>/SKILL.md` entries
- `ramp-skills.tar.gz.sha256`, containing the archive's SHA-256 digest

Release tags must contain only letters, digits, `.`, `_`, and `-`. The CLI
discovers the default release through GitHub's `releases/latest` API; callers
can pin an immutable release with `ramp skills update --version <tag>` or
`RAMP_SKILLS_VERSION=<tag>`. Downloads must match both GitHub's immutable
release-asset digest and the published checksum sidecar before extraction. If
no verified catalog is cached, a skills command downloads the latest release;
interactive installs offer newer releases when a catalog is already active.
The legacy CLI names `apply-to-ramp` and `incorporate-with-ramp` continue to map
to their renamed public skills for backwards compatibility.

### Via npx

```bash
# Install one skill (project-level)
npx skills add ramp-public/skills --skill ramp-receipt-compliance

# Install all skills globally
npx skills add -g ramp-public/skills
```

Or copy the skill folder into `.claude/skills/` (Claude Code), `.codex/skills/` (Codex), `.cursor/skills/` (Cursor), or your agent's skill directory.

## Prerequisites

The `ramp` CLI must be installed and authenticated:
```bash
npm install -g @ramp/cli
ramp auth login
ramp tools refresh   # fetch latest tool aliases
```

## Flag conventions

- All CLI flags use **underscores**, not hyphens (e.g. `--page_size`, not `--page-size`).
- Skills must invoke agent-tool commands with `--agent` and a `--rationale` (see below).
- **`--page_size` / `--limit`**: Paginated commands use either `--page_size` or `--limit` depending on the endpoint. The CLI adds a bidirectional alias so both names are accepted on every command that has either flag. Use whichever reads more naturally.

## Agent-tool calls require `--agent` and `--rationale`

Every `ramp <resource> <tool>` command maps to an agent-tool endpoint. Skills
must run these commands with `--agent`; the Developer API requires a rationale
on each call. The CLI exposes it as the `--rationale` flag (or a `"rationale"`
key when you use `--json`).

- **Required for every skill invocation** — use `--agent` and pass a non-empty rationale explicitly.
- **Constraints:** a non-empty string, **max 1024 characters**. An empty string (`--rationale ""`) is rejected.
- **What it is:** a brief, human-readable reason for the call — what goal or workflow it serves and what you intend to do with the result (e.g. `--rationale "Categorize the user's March transactions"`).
- **If you omit it**, the API returns:

  ```
  HTTP 422 — API error 422: There was an error.
  ```

  This is `DEVELOPER_7001` / `DEVELOPER_INVALID_SCHEMA`, a request-body validation error — **not** a permissions or account-enablement problem. A Read+Write key with a malformed body fails the same way. `--dry_run` only prints the outgoing body and does **not** validate it, so a body that dry-runs cleanly can still 422 if `rationale` is missing.

  ```bash
   # Skills must use --agent and provide a rationale
   ramp --agent transactions missing {transaction_uuid} --rationale "Check missing items before cleanup"
  ```

Always pass `--agent` and `--rationale` (or include `"rationale"` in the `--json` body) when a skill invokes an agent-tool command.

## How skills work

Skills are instruction files that load on demand. The agent reads the SKILL.md, runs CLI commands via shell, and interprets the results. No MCP server, no tool schemas, no running processes.

The core `ramp` skill covers general commands. These workflow skills layer on
top — teaching the agent how to combine commands into action-oriented
workflows.

## Build your own

Copy any skill folder, edit the SKILL.md to match your workflow. The format is simple:
1. YAML frontmatter: `name` and `description` (controls when the agent loads it)
2. Commands to run
3. Workflow instructions (what to do, how to present results, gotchas)

See [agentskills.io/specification](https://agentskills.io/specification) for the full spec.
