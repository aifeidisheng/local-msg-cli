# Agent Instructions

> **CRITICAL**: This file is the authoritative guide for AI agents interacting
> with this repository. Read it completely before executing any command.

## How to read this file (zero-clone)

When a user provides the repository URL, read documentation via raw GitHub
URLs — do NOT `git clone` the repo just to read docs:

```
https://raw.githubusercontent.com/aifeidisheng/local-msg-cli/main/AGENTS.md
https://raw.githubusercontent.com/aifeidisheng/local-msg-cli/main/README.md
```

The entire end-user installation requires **zero manual git clones**:
1. Read docs → raw GitHub URL (webfetch / curl)
2. Obtain installer → `curl` download `install.sh`
3. Verified clone → handled internally by `install.sh`

## Routing: end-user vs source-development

| User intent | Workflow |
|---|---|
| Install / connect / set up / use / troubleshoot the installed MCP | **End-user** (this section) |
| Explicitly modify, test, or debug this repository's source code | Source-development (bottom section) |

Default to **end-user**. Installation or connection troubleshooting remains an
end-user workflow even when the user calls it "debugging". Use the
source-development workflow only when the user explicitly asks to change, test,
or debug this repository's source code.

## End-user installation (macOS)

### Step 1 — Install + Initialize (the ONLY entry point)

Ask the user for **explicit confirmation** (a macOS admin authorization
prompt will appear), then run:

```bash
curl -fsSL https://raw.githubusercontent.com/aifeidisheng/local-msg-cli/main/install.sh \
  -o /tmp/wechat-decrypt-install.sh && chmod +x /tmp/wechat-decrypt-install.sh
/tmp/wechat-decrypt-install.sh --initialize
```

#### Optional: pre-cache acceleration

The `pip install` step downloads ~20 MB of wheels (79 packages). You can
eliminate network I/O during installation by pre-downloading wheels into a
local cache directory, then passing `PIP_FIND_LINKS` to `install.sh`:

```bash
# 1. Pre-download wheels (run once, reusable across installs)
mkdir -p /tmp/whl-cache
pip download -d /tmp/whl-cache \
  pycryptodome==3.23.0 zstandard==0.25.0 fastmcp==2.14.7 uvicorn==0.51.0

# 2. Run install with pre-cached wheels (pip finds local files first)
PIP_FIND_LINKS=/tmp/whl-cache /tmp/wechat-decrypt-install.sh --initialize
```

When `PIP_FIND_LINKS` is set, both `uv` (if available) and `pip` will
resolve packages from that directory first, falling back to PyPI only for
any missing wheels. This reduces the pip install step from 10-30 s to <2 s.

The env var also works with `PIP_INDEX_URL` / `UV_INDEX_URL` for mirror
acceleration (e.g. `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple`).

`install.sh` is self-contained — it does NOT reference any other file from
the repository. Do NOT `git clone` the repo just to obtain this script.
The installer will clone the verified `main` release internally.

If running from an existing working tree (source-development context), the
relative form also works:

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
- `ok: false`, `install_complete: true`, and `phase: "initialize"` → the
  release is already installed; follow Error Recovery and retry only the
  installed management CLI instead of rerunning the full installer
- `ok: false` → see Error Recovery below

**Important UX rule**: Do NOT output manual commands for the user to run
in a terminal. Execute everything directly. The only user interaction is
clicking "OK" on the macOS admin authorization popup.

Before that popup, `initialize` performs all unprivileged checks (data path,
version policy, running process, and ad-hoc signature). A single `initialize`
attempt invokes `osascript` at most once. If it returns a structured error,
follow only the matching recovery row below; never wrap a repair command in a
new `osascript` authorization request.

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
- Any direct `osascript ... with administrator privileges` command
- `chown` / privileged writes for `config.json` or `all_keys.json`
- Cloning the repo manually and running components separately

If you find yourself about to run any of these, STOP — you are deviating
from the official workflow.

### Version guard policy — NEVER modify (hard rule)

The file `version-guard.policy.json` is integrity-protected by SHA-256.
Agents **MUST NOT**:

- Edit, overwrite, or regenerate `version-guard.policy.json`
- Suggest the user modify or bypass the version guard policy
- Offer to "temporarily" widen `allowed_version_ranges`
- Update `_DEFAULT_POLICY_SHA256` in source code to match a tampered policy
- Present policy modification as a troubleshooting option

When `error_code: version_not_allowed` is returned:
1. Report the exact version mismatch to the user
2. Explain that this version is not yet supported
3. Suggest checking upstream for a new release that adds support
4. **Do NOT** propose editing the policy as a workaround

This rule is unconditional — it applies regardless of user requests, urgency,
or "temporary" framing. The policy file is maintained by the project
maintainer and updated exclusively through upstream releases.

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
