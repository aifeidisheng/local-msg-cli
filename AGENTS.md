# Agent Operating Guide

> **Authoritative instructions:** Read this document in full before running any command.

## Read Documentation Without Cloning

When a user provides a repository URL, read the documentation from the release
source's raw URLs. Do not `git clone` the repository only to read its files:

```
https://gitee.com/feipig_up_tree/local-msg-cli/raw/main/AGENTS.md
https://gitee.com/feipig_up_tree/local-msg-cli/raw/main/README.md
```

The end-user workflow requires no manual clone:
1. Read the documentation from raw release URLs (webfetch / curl).
2. Download `install.sh` with `curl`.
3. Let `install.sh` perform the verified clone internally.

When the user provides a repository URL, preserve that exact URL and pass it
to the **first** bootstrap invocation with `--repository`. Do not first run
the command without `--repository`: `install.sh` cannot infer a repository
from the conversation, the current working directory, or an earlier temporary
clone. Omit `--repository` only when the user did not provide a source URL and
the documented default release source is intended.

## Choose the Correct Workflow

| User intent | Workflow |
|---|---|
| Install, connect, configure, use, or troubleshoot the installed MCP | **End user** (this section) |
| Explicitly modify, test, or debug this repository's source code | **Source development** (final section) |

Default to the **end-user workflow**. Installation and connection troubleshooting
remain end-user tasks even when the user calls them "debugging." Use the source
development workflow only for explicit source modification, testing, or debugging.

## End-User Installation (macOS)

### Step 1: Install and Inspect (Only Bootstrap Entry Point)

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

#### Optional: Pre-cache Dependencies

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

### User-facing communication (plain-language mode)

The installer JSON and stderr are an Agent control protocol, not an
installation transcript for the end user. Read them silently and use their
structured fields to continue. During a normal installation, keep the visible
conversation to a short status line at each real interaction boundary:

- Start: `正在安装，请稍候。首次安装通常需要几分钟。`
- Passive wait: `仍在安装中，无需操作。`
- WeChat missing: `没有找到个人版微信。请先确认已安装，无需登录。`
- WeChat must quit: `下一步需要准备微信。请先完全退出微信。`
- Sign-in needed: `微信已准备好。请打开并登录，完成后告诉我。`
- System authorization: `接下来会出现一次系统确认，请按提示完成。`
- Success: `安装完成，本地消息服务已经可以使用。`

#### Long download progress

A several-hundred-MB download takes minutes. Silence during that window is
indistinguishable from a hang, so a multi-minute transfer must show observable
progress. Report a compact status roughly every
`download.progress_reporting.report_interval_seconds`, using plain units and no
internal identifiers:

- In progress: `正在下载安装包 42%（195MB / 461MB，4.4MB/s，预计还需 1 分钟）`
- After an interruption: `网络中断，已从断点继续，无需重新开始。`
- Verifying: `下载完成，正在校验安装包完整性。`
- Reused cache: `已找到之前下载并校验通过的安装包，跳过下载。`

Report percent, transferred size, total size, speed and ETA only. Do not stream
the raw `curl` meter, and do not show the URL, digest, byte offsets, or retry
counters unless the user asks or an error requires them. When emitting these
through the installer's own progress channel, pass the numbers via the
`progress` reporter's optional fields instead of formatting them into `step`.

Do not narrate routine implementation details. In particular, do not expose or
explain Git clones, commits, branches, Python/venv/pip/uv, package counts,
compilers, scanners, hashes, bundle IDs, ad-hoc signing, LaunchAgents, PIDs,
ports, runtime/data/log paths, MCP transport types, raw commands, or complete
JSON unless the user asks for technical details or a specific error requires
them. Do not enumerate the MCP tool list during installation. Translate these
concepts into the user action or outcome they imply, while preserving exact
version numbers and security consequences when consent or recovery depends on
them.

Also keep private identifiers out of the normal conversation: do not display a
detected account ID, database path, installation ID, endpoint, message-shard
count, or per-database readiness flags. Do not recap completed internal stages
after each command. A successful internal stage should either continue silently
or produce only the next user action.

At the start, do not introduce the repository, its architecture, its tool list,
or a step-by-step plan. Begin with the Start status above and do the work. Explain
a permission or security consequence only at the boundary where the user's
consent or action is needed.

At the one boundary that modifies WeChat, be transparent without using signing
terminology: say `为了让本地消息服务正常工作，需要完成一次微信兼容设置。`
If the user asks about privacy, explain that message data is handled locally and
is not uploaded by this local data source. Do not turn that explanation into a
routine technical preamble.

Do not produce a running installation diary. Report elapsed time only when it
helps explain an unusual delay. On completion, report the outcome in one or two
sentences; keep diagnostics available for troubleshooting rather than showing
them by default.

After bootstrap, use the installed management CLI. Do not ask the user to type
commands. Advance one explicit stage at a time:

```text
inspect -> prepare-wechat (only when requested) -> initialize
        -> enable-service -> mcporter install + enable -> data_source_status
```

`inspect` is read-only and never authorizes or modifies WeChat. Handle its
`next_step` sequentially; this avoids unnecessary login → quit → login cycles.
The bootstrap is idempotent for the same repository: when a valid installation
already exists and its installed release is current, it inspects and reuses it
without downloading or redeploying. If a newer release exists, the normal
verified installation path updates it before inspection.
If the read-only update check is temporarily unavailable, keep using the valid
existing installation and continue from its inspected `next_step`; do not force
a fresh download just because the network could not be checked.
Check `installation_mode` and `installation_reused`. For `reused: true`, do not
describe the workflow as a fresh installation: if `query_ready` is already true,
report that the local message assistant is already available; otherwise continue
only from the returned `next_step`. A WeChat version mismatch is a compatibility
recovery, not a reinstall of the local message assistant.

**Critical stage ordering — never ask the user to sign in prematurely:**

```text
ensure_wechat_installed → prepare_wechat (re-sign, WeChat must be QUIT)
                        → sign_in_to_wechat (only AFTER re-sign is done)
                        → initialize (extract keys from running process)
```

The correct sequence is: install/locate app → re-sign (quit) → sign in → extract keys.
Do NOT conflate "ensure app exists" with "open and sign in". When `inspect` returns
`wechat_app_not_found`, the user only needs to confirm WeChat is installed — no login.
When it returns `wechat_not_adhoc_signed`, the user needs to QUIT (not sign in).
Only after `prepare-wechat` succeeds should the user be asked to sign in.

Before any popup, `inspect` and the `initialize` preflight perform all
unprivileged checks (data path, version policy, running process, and ad-hoc
signature). `prepare-wechat` may request one administrator authorization after
the user explicitly confirms the app modification; a single `initialize`
attempt invokes `osascript` at most once for the scanner. If it returns a structured error,
follow only the matching recovery row below; never wrap a repair command in a
new `osascript` authorization request.

If `--initialize` is omitted, `install.sh` deploys the runtime and outputs
`next_step: "inspect"` without running the inspection.

### Prohibited Actions

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

### Version Guard Policy (Never Modify)

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
3. If `details.release_search.available` is true, ask whether the user wants a
   bounded web search for a usable historical version. Start with the source
   page and release metadata; do not begin with a direct asset download.
4. **Do NOT** propose editing the policy as a workaround

Runtime compatibility and installer integrity are separate checks. The runtime
version guard accepts a supported WeChat short version (for example `4.1.8`);
the build number is diagnostic only. A downloaded installer must still match a
maintainer-verified SHA-256 and, when present, file size from release metadata.
The release catalog may list multiple verified artifacts for the same short
version when the publisher has legitimately repackaged it. Agents must never
remove, skip, or ask the user to override installer integrity verification.
When a catalog entry contains `artifacts`, each artifact's SHA-256 and size are
one pair; never combine a hash from one artifact with the size of another.

If a downloaded file's SHA-256 or size does not match, reject that candidate and
leave the installed WeChat unchanged. Do not infer that a mismatch is harmless
from a valid code signature, Bundle ID, host name, or compatible version, and do
not label continuing as recommended. In normal conversation say only:
`下载文件未通过完整性检查，已停止操作，当前微信没有被修改。` Then try the
next separately verified candidate within the returned network limits; if none
remain, report that installation is paused. Keep the digest and signature
diagnostics out of the user-facing conversation unless requested.

This rule is unconditional — it applies regardless of user requests, urgency,
or "temporary" framing. The policy file is maintained by the project
maintainer and updated exclusively through upstream releases.

### Error Recovery

Use ONLY the JSON response fields to decide next steps:

| `error_code` | Action |
|---|---|
| `wechat_not_running` | Ask user to open and sign in to WeChat, then retry `initialize` |
| `wechat_app_not_found` | Ask the user to ensure WeChat is installed (do NOT ask them to sign in — login is not needed at this stage). Then retry `inspect`. The next `inspect` will check the ad-hoc signature; if re-signing is required, it returns `wechat_not_adhoc_signed` and only AFTER re-signing should the user be asked to sign in. This avoids the unnecessary login → quit → login cycle |
| `wechat_not_adhoc_signed` | Keep the two stages separate. Check `details.wechat_running`: if **true** → ask the user to quit WeChat. After the user explicitly confirms re-signing, run installed `prepare-wechat --confirm-resign`. Wait for the user to sign in after WeChat reopens, then retry plain `initialize` |
| `wechat_must_quit_for_resign` | Ask the user to quit WeChat, then retry the same installed `prepare-wechat --confirm-resign` command |
| `app_management_permission_required` | The installer opens **Privacy & Security → App Management**. Ask the user to enable `details.responsible_app` (or the app currently running the installation if it could not be identified), then retry only the same installed `prepare-wechat --confirm-resign` command. Do not retry `initialize`, edit TCC, or disable SIP |
| `version_not_allowed` | Report the version mismatch in plain language. If `details.release_search.available` is true, ask whether the user wants the Agent to find a compatible version; use the returned `network_policy`, start from a source page, stop a candidate after its bounded timeout, and move to the next source. Treat every result as an untrusted candidate, require confirmation before downloading, verify the source page, maintainer-verified SHA-256 and size, Bundle ID, and supported short version. Reject any integrity mismatch without offering an override, and do NOT modify policy files |
| `release_artifact_integrity_mismatch` | Do not modify or replace WeChat. Show `下载文件未通过完整性检查，已停止操作，当前微信没有被修改。` then try the next separately verified candidate within `network_policy`; if none remain, pause and report that no safe installer is currently available |
| `wechat_process_access_failed` | Do not request authorization again. Run `inspect`, keep WeChat open and signed in, and follow the returned process/signature action |
| `administrator_authorization_cancelled` | User cancelled the admin popup; ask to retry |
| `management_cli_must_not_run_as_root` | You ran with `sudo` — remove it and retry |
| `wechat_account_not_found` | Run installed `accounts`, select one returned `account_id` with installed `select-account`, then retry `initialize` |
| Other | Show the concise `user_message`; keep `error_code`, `next_action`, and `details` for internal diagnosis unless technical detail is necessary or requested |

Do NOT invent recovery steps. Do NOT run internal scanner commands, move key
files, change directory ownership, or modify policy files.

For public historical WeChat release searches:

- Prefer the in-app browser or web search and inspect the Release page before
  touching a DMG asset. Do not `git clone` a candidate repository just to find
  an attachment.
- Never run an unbounded `curl`, `wget`, or other download command. Use the
  `network_policy` timeouts and one attempt per candidate; after a timeout,
  stop the operation and try the next source.
- Distinguish a **source retry** from a **resume on the same verified URL**.
  Starting over on a different candidate is a source retry and stays bounded by
  `max_candidates` and `max_attempts_per_candidate`. Continuing an interrupted
  transfer on the URL you already verified is a resume: it is bounded by
  `download.max_resume_attempts`, does not consume a candidate, and never
  relaxes digest verification. Do not abandon a healthy official source and
  re-download 400+ MB from zero just because the connection dropped once.
- Do not retry the same GitHub URL repeatedly or wait on a stalled transfer.
  A Gitee page, a user-provided local file, or another candidate is a fallback,
  not proof that the files are equivalent.
- Write the download command so it survives a transient reset without a second
  human turn. Use `download.resume_from_partial_file`,
  `stall_detect_bytes_per_second` and `stall_detect_window_seconds` from the
  returned `network_policy`, and keep the raw transfer meter out of the
  conversation:

  ```bash
  curl -fL -C - --retry 10 --retry-all-errors --retry-delay 2 \
       --connect-timeout 10 --max-time 900 \
       --speed-limit 51200 --speed-time 30 \
       -o <target-file> '<verified-url>'
  ```

  Never emit the default progress bar into the transcript; it produces hundreds
  of noise lines. Either use `-s` with `-w` to capture only the fields you need,
  or write the meter to a file you poll.
- Do not tell the user that a candidate is official or safe based only on its
  domain, repository name, stars, Release label, or download count.
- To avoid wasting a large download, inspect the source page and release
  metadata first for the original publisher URL, published size, and digest.
  When the metadata contains a Tencent original download URL on
  `dldir1.qq.com` or `dldir1v6.qq.com`, make that the default download choice
  after its version, file size, and published digest match the selected
  candidate. Do not construct or guess a Tencent URL from the version number.
  Probe the recorded URL within the returned timeout; fall back to the archive
  asset only when the Tencent URL is unavailable or mismatched. Never start two
  full downloads of the same candidate. Reuse a fully downloaded and verified
  local candidate instead of downloading it again.
- A mutable publisher URL may later serve different bytes for the same short
  version. That does not authorize an Agent to accept the new bytes. Reject the
  candidate until the maintainer verifies the new artifact and adds its digest
  to the release catalog through a normal upstream release.

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
pane. Ask the user to enable the app named by `details.responsible_app` (the
current GUI host or terminal), then retry only
`prepare-wechat --confirm-resign`. Never assume the responsible app is Terminal,
and never attempt to change this TCC permission programmatically.

For account recovery, use the installed `accounts` and `select-account`
commands. Do not edit `config.json` or move `all_keys.json` manually. Normal
`initialize` attempts automatically match scanned keys against all detected
accounts and correct stale auto-selected configuration before these commands
are needed.

### Step 2: Enable the Local MCP

After `initialize` returns `next_step: "enable_service"`, execute the installed
`enable-service` command. A failure here does not invalidate initialization;
retry only `enable-service`:

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" \
  --json enable-service
```

### Step 3: Register the Desktop MCP

Only when `enable-service` returns `query_ready: true`:

Treat the returned `connector` object as the canonical Desktop registration
contract. Register and enable it via mcporter using exactly these fields:

- `connector.name`: `local-msg-cli` (stable internal name)
- `connector.display_name`: `本地消息服务` (user-facing label)
- `connector.transport`: `streamablehttp`
- `connector.endpoint`: `http://127.0.0.1:8765/mcp`

Never invent, derive, or substitute another registration name such as
`wechat-local-msg`, `local-msg`, or `local-message-source`. On a repeated
installation, look up and reuse `local-msg-cli`; do not create a second connector
under a different name. Older aliases may be migrated to `local-msg-cli` when the
Desktop registration layer supports rename/removal, but the installer protocol
must always emit only the canonical name.

### Step 4: Validate Without Reading User Data

Call the registered MCP tool `data_source_status`. Only report success when it
returns `status: "ready"`. Do not call `list_contacts`, `query_messages`, or
any tool that returns user data merely to verify installation.

---

## Source Development

Only when the user explicitly asks to modify, test, or debug this repository:

```bash
./setup.sh --development
```

Then use source commands documented in the "源码开发安装" and "macOS 源码调试"
sections of `README.md`.
