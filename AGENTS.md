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

### Step 1 — Install + Initialize (the ONLY entry point)

Ask the user for **explicit confirmation** (a macOS admin authorization
prompt will appear), then run:

```bash
./install.sh --initialize
```

This single command does everything:
1. Clones the verified `main` release
2. Creates an isolated venv and installs dependencies
3. Compiles and signs the macOS key scanner
4. Installs a LaunchAgent (auto-start on login)
5. Extracts DB keys via macOS system authorization (admin popup)
6. Pre-decrypts databases so MCP becomes `query_ready`

The command outputs a single JSON line to stdout. Check:
- `ok: true` and `query_ready: true` → proceed to Step 2
- `ok: false` → see Error Recovery below

**Important UX rule**: Do NOT output manual commands for the user to run
in a terminal. Execute everything directly. The only user interaction is
clicking "OK" on the macOS admin authorization popup.

If `--initialize` is omitted, install.sh only does steps 1-4 and outputs
`next_step: "run_init_with_user_confirmation"`.

### Prohibited actions (hard blocklist)

Do NOT perform any of the following during end-user installation:

- `pip install -r requirements.txt` in the checkout
- `python3 -m venv .venv` in the checkout
- `cc -O2 -o find_all_keys_macos ...` (manual compile)
- `sudo ./find_all_keys_macos` (manual key extraction)
- `python main.py init` / `python main.py serve` from the checkout
- `setup.sh` / `setup.py` / `service.py install`
- Any `sudo` command prefixed to the management CLI
- Cloning the repo manually and running components separately

If you find yourself about to run any of these, STOP — you are deviating
from the official workflow.

### Error recovery

Use ONLY the JSON response fields to decide next steps:

| `error_code` | Action |
|---|---|
| `wechat_not_running` | Ask user to open WeChat and retry `initialize` |
| `wechat_not_adhoc_signed` | Ask user to quit WeChat, run `sudo codesign --force --deep --sign - /Applications/WeChat.app`, reopen WeChat, then retry |
| `version_not_allowed` | Report the version mismatch; do NOT modify policy files |
| `task_for_pid_failed` | The system auth prompt was denied; ask user to retry and approve |
| `administrator_authorization_cancelled` | User cancelled the admin popup; ask to retry |
| `management_cli_must_not_run_as_root` | You ran with `sudo` — remove it and retry |
| Other | Report `error_code` + `next_action` text to user |

Do NOT invent recovery steps. Do NOT run internal scanner commands, move key
files, change directory ownership, or modify policy files.

To retry initialization after fixing the issue:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" --json initialize
```

### Step 2 — Register Desktop MCP

Only when Step 1 JSON shows `query_ready: true`:

Register `http://127.0.0.1:8765/mcp` as a `streamablehttp` MCP in the
Desktop connector (via mcporter install + enable).

### Step 3 — Validate (no user data)

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
