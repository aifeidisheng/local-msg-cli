# Agent Instructions

> **CRITICAL**: This file is the authoritative guide for AI agents interacting
> with this repository. Read it completely before executing any command.

## How to read this file (zero-clone)

When a user provides the repository URL, read documentation via the release
source's raw URLs — do NOT `git clone` the repo just to read docs:

```
https://gitee.com/feipig_up_tree/local-msg-cli/raw/main/AGENTS.md
https://gitee.com/feipig_up_tree/local-msg-cli/raw/main/README.md
```

The entire end-user installation requires **zero manual git clones**:
1. Read docs → raw GitHub URL (webfetch / curl)
2. Obtain installer → `curl` download `install.sh`
3. Verified clone → handled internally by `install.sh`

When the user provides a repository URL, preserve that exact URL and pass it
to the **first** bootstrap invocation with `--repository`. Do not first run
the command without `--repository`: `install.sh` cannot infer a repository
from the conversation, the current working directory, or an earlier temporary
clone. Omit `--repository` only when the user did not provide a source URL and
the documented default release source is intended.

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

### Step 1 — Install + Inspect (the ONLY bootstrap entry point)

Run the bootstrap directly. This stage is unprivileged and must not show a
macOS administrator authorization prompt. Desktop hosts should pass their
bundled Python explicitly through `--python` (or `WECHAT_DECRYPT_PYTHON`) so
the user does not lose a retry when system `PATH` has no Python:

```bash
curl -fsSL https://gitee.com/feipig_up_tree/local-msg-cli/raw/main/install.sh \
  -o /tmp/wechat-decrypt-install.sh && chmod +x /tmp/wechat-decrypt-install.sh
/tmp/wechat-decrypt-install.sh --repository '<user-provided-repository-url>' --initialize
```

If no repository URL was provided, omit `--repository` and use the default
release source. Never clone the repository separately to establish the source
for `install.sh`; the bootstrap performs its own verified clone.

When the Desktop host exposes Python 3.10+, use the managed invocation
`install.sh --repository <repository-url> --python <host-python> --initialize`.
The installer still creates
its own isolated runtime; the host interpreter is only used to bootstrap it.
Without an explicit path, the script checks supported Python bundles under
`/Applications` and `~/Applications`, then falls back to `python3.13` through
`python3`.

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

This compatibility entry point deliberately stops at the next interaction
boundary. It:
1. Clones the verified `main` release
2. Creates an isolated venv and installs dependencies
3. Compiles and signs the macOS key scanner
4. Deploys the fixed runtime and stable management CLI
5. Runs the read-only `inspect` command

The command outputs a single JSON line to stdout. Check:
- `ok: true` → follow only its `next_step`
- `install_complete: true` → never rerun the bootstrap for a later-stage error
- `ok: false` → see Error Recovery below

**Important UX rule**: Do NOT output manual commands for the user to run
in a terminal. Execute everything directly. User interaction is limited to
confirming sensitive actions, quitting/signing in to WeChat when requested,
and clicking "OK" on the macOS admin authorization popup.

After bootstrap, use the installed management CLI. Do not ask the user to type
commands. Advance one explicit stage at a time:

```text
inspect -> prepare-wechat (only when requested) -> initialize
        -> enable-service -> mcporter install + enable -> data_source_status
```

`inspect` is read-only and never authorizes or modifies WeChat. Handle its
`next_step` sequentially; this avoids unnecessary login → quit → login cycles.

Before any popup, `inspect` and the `initialize` preflight perform all
unprivileged checks (data path, version policy, running process, and ad-hoc
signature). `prepare-wechat` may request one administrator authorization after
the user explicitly confirms the app modification; a single `initialize`
attempt invokes `osascript` at most once for the scanner. If it returns a structured error,
follow only the matching recovery row below; never wrap a repair command in a
new `osascript` authorization request.

If `--initialize` is omitted, `install.sh` deploys the runtime and outputs
`next_step: "inspect"` without running the inspection.

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
- Any direct `sudo codesign ... WeChat.app` command; use the installed
  `prepare-wechat` management command after explicit user confirmation
- Any attempt to edit the TCC database, disable SIP, or automate the App
  Management toggle; macOS requires the user to grant this permission
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
| `wechat_not_running` | Ask user to open and sign in to WeChat, then retry `initialize` |
| `wechat_app_not_found` | Ask the user to install or open WeChat, then retry `inspect`; do not report a version-policy failure |
| `wechat_not_adhoc_signed` | Keep the two stages separate. Check `details.wechat_running`: if **true** → ask the user to quit WeChat. After the user explicitly confirms re-signing, run installed `prepare-wechat --confirm-resign`. Wait for the user to sign in after WeChat reopens, then retry plain `initialize` |
| `wechat_must_quit_for_resign` | Ask the user to quit WeChat, then retry the same installed `prepare-wechat --confirm-resign` command |
| `app_management_permission_required` | The installer opens **Privacy & Security → App Management**. Ask the user to enable `details.responsible_app` (or the app currently running the installation if it could not be identified), then retry only the same installed `prepare-wechat --confirm-resign` command. Do not retry `initialize`, edit TCC, or disable SIP |
| `version_not_allowed` | Report the version mismatch; do NOT modify policy files |
| `wechat_process_access_failed` | Do not request authorization again. Run `inspect`, keep WeChat open and signed in, and follow the returned process/signature action |
| `administrator_authorization_cancelled` | User cancelled the admin popup; ask to retry |
| `management_cli_must_not_run_as_root` | You ran with `sudo` — remove it and retry |
| `wechat_account_not_found` | Run installed `accounts`, select one returned `account_id` with installed `select-account`, then retry `initialize` |
| Other | Report `error_code` + `next_action` text to user |

Do NOT invent recovery steps. Do NOT run internal scanner commands, move key
files, change directory ownership, or modify policy files.

To retry initialization after fixing the issue:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" --json initialize
```

**Required separate re-sign stage:** After the user has quit WeChat and
explicitly confirmed the app modification, run the standalone re-sign command.
It validates the detected app's bundle identity, authorizes only the fixed
attribute-cleanup and `codesign` operations, verifies the new signature, and
reopens WeChat:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" \
  --json prepare-wechat --confirm-resign
```

Do not pass `--confirm-resign` to `initialize`. After `prepare-wechat` returns
success, wait for the user to finish signing in, then retry the plain
`initialize` command shown above. This boundary prevents re-signing and key
extraction from racing the same WeChat shutdown/restart.

On newer macOS releases, modifying another app bundle also requires **App
Management** permission for the GUI app responsible for the installation. This
is separate from the administrator password prompt. If macOS denies the fixed
re-sign command with `Operation not permitted`, `prepare-wechat` returns
`app_management_permission_required` and opens the correct System Settings
pane. Ask the user to enable the app named by `details.responsible_app` (for
example ChatGPT, Terminal, or the IDE/agent host), then retry only
`prepare-wechat --confirm-resign`. Never assume the responsible app is Terminal,
and never attempt to change this TCC permission programmatically.

For account recovery, use the installed `accounts` and `select-account`
commands. Do not edit `config.json` or move `all_keys.json` manually. Normal
`initialize` attempts automatically match scanned keys against all detected
accounts and correct stale auto-selected configuration before these commands
are needed.

### Step 2 — Enable the Local MCP

After `initialize` returns `next_step: "enable_service"`, execute the installed
`enable-service` command. A failure here does not invalidate initialization;
retry only `enable-service`:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" \
  --json enable-service
```

### Step 3 — Register Desktop MCP

Only when `enable-service` returns `query_ready: true`:

Register `http://127.0.0.1:8765/mcp` as a `streamablehttp` MCP in the
Desktop connector (via mcporter install + enable).

### Step 4 — Validate (no user data)

Call the registered MCP tool `data_source_status`. Only report success when it
returns `status: "ready"`. Do not call `list_contacts`, `query_messages`, or
any tool that returns user data merely to verify installation.

---

## Source-development workflow

Only when the user explicitly asks to modify, test, or debug this repository:

```bash
./setup.sh --development
```

Then use source commands documented in the "源码开发安装" and "macOS 源码调试"
sections of `README.md`.
