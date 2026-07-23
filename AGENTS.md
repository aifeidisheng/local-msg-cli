# Agent Instructions

> **CRITICAL**: This file is the authoritative guide for AI agents interacting
> with this repository. Read it completely before executing any command.

## Routing: end-user vs source-development

| User intent | Workflow |
|---|---|
| Install / connect / set up / use the MCP | **End-user** (this section) |
| Modify / test / debug / develop the source | Source-development (bottom section) |

Default to **end-user** unless the user explicitly says "develop", "debug",
or "modify the source code".

## End-user installation (macOS)

### Step 1 — Bootstrap (the ONLY entry point)

```bash
./install.sh
```

This single command does everything: clones the verified `main` release,
creates an isolated venv, compiles the key scanner, installs a LaunchAgent,
and generates the management CLI. **No other manual steps are needed or
allowed.**

### Prohibited actions (hard blocklist)

Do NOT perform any of the following during end-user installation:

- `pip install -r requirements.txt` in the checkout
- `python3 -m venv .venv` in the checkout
- `cc -O2 -o find_all_keys_macos ...` (manual compile)
- `sudo ./find_all_keys_macos` (manual key extraction)
- `python main.py init` / `python main.py serve` from the checkout
- `setup.sh` / `setup.py` / `service.py install`
- Any `sudo` command prefixed to the management CLI

If you find yourself about to run any of these, STOP — you are deviating
from the official workflow.

### Step 2 — Sensitive initialization (requires user confirmation)

After `install.sh` succeeds, ask for **explicit user confirmation** before
running this command. Do NOT prepend `sudo`:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" initialize --json
```

This reads WeChat process memory via a system authorization prompt (not
terminal sudo). The command returns machine-readable JSON.

### Step 3 — Error recovery

Use ONLY the JSON response fields to decide next steps:

| `error_code` | Action |
|---|---|
| `wechat_not_running` | Ask user to open WeChat and retry |
| `wechat_not_adhoc_signed` | Ask user to quit WeChat, run `sudo codesign --force --deep --sign - /Applications/WeChat.app`, reopen WeChat, then retry |
| `version_not_allowed` | Report the version mismatch; do NOT modify policy files |
| `task_for_pid_failed` | The system auth prompt was denied; ask user to retry and approve |
| `management_cli_must_not_run_as_root` | You ran with `sudo` — remove it and retry |
| Other | Report `error_code` + `next_action` text to user |

Do NOT invent recovery steps. Do NOT run internal scanner commands, move key
files, change directory ownership, or modify policy files.

### Step 4 — Verify readiness

Run status check:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" status --json
```

Only proceed when the JSON contains:
- `service.status` = `"ready"`
- `service.query_ready` = `true`

`waiting_for_wechat` means the LaunchAgent is healthy but MCP is not yet
queryable — do NOT register the MCP endpoint in this state.

### Step 5 — Register Desktop MCP

Register `http://127.0.0.1:8765/mcp` as a `streamablehttp` MCP in the
Desktop connector.

### Step 6 — Validate (no user data)

Call the MCP tool `data_source_status`. Only report success when it returns
`status: "ready"`. Do NOT call `list_contacts`, `query_messages`, or any
tool that returns user data merely to verify installation.

---

## Source-development workflow

Only when the user explicitly asks to modify, test, or debug this repository:

```bash
./setup.sh --development
```

Then use source commands documented in the "源码开发安装" and "macOS 源码调试"
sections of `README.md`.
