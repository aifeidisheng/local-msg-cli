#!/usr/bin/env bash
# Canonical bootstrap for an end-user macOS installation.
# This script is SELF-CONTAINED — it does not reference any other file from
# the repository. It can be downloaded standalone (e.g. via curl) and executed
# directly; it will clone the verified main release internally.
set -euo pipefail

# The default is used only when the caller did not provide a repository URL.
# A caller-supplied URL must be passed explicitly with --repository; this
# script cannot infer it from conversation context or a previous clone.
readonly DEFAULT_REPOSITORY="https://gitee.com/feipig_up_tree/local-msg-cli.git"
readonly RELEASE_BRANCH="main"
readonly MANAGEMENT_CLI="$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light"
readonly INSTALL_MANIFEST="$HOME/Library/Application Support/WeChatDecryptLight/install.json"

repositories=("${WECHAT_DECRYPT_REPOSITORY:-$DEFAULT_REPOSITORY}")
python_bin="${WECHAT_DECRYPT_PYTHON:-}"
do_initialize=false
result_emitted=false

command_name() {
    if [[ "$do_initialize" == true ]]; then
        printf '%s' 'install+inspect'
    else
        printf '%s' 'install'
    fi
}

emit_error_json() {
    local error_code="$1"
    local phase="$2"
    local error_message="$3"
    local next_action="$4"
    local user_message="$error_message"
    case "$error_code" in
        unsupported_platform) user_message="当前设备暂不支持此安装方式。" ;;
        invalid_arguments) user_message="安装参数不完整，请稍后重试。" ;;
        all_git_sources_unreachable) user_message="暂时无法下载安装文件，请检查网络后重试。" ;;
        installer_bootstrap_failed|install_output_invalid) user_message="安装暂时没有完成，请稍后重试。" ;;
    esac
    result_emitted=true
    printf '{"ok":false,"command":"%s","phase":"%s","error_code":"%s","error":"%s","user_message":"%s","next_action":"%s"}\n' \
        "$(command_name)" "$phase" "$error_code" "$error_message" "$user_message" "$next_action"
}

unexpected_error() {
    local exit_code=$?
    local line_number="$1"
    if [[ "$result_emitted" != true ]]; then
        echo "Unexpected installer bootstrap failure at line $line_number (exit $exit_code)." >&2
        emit_error_json \
            "installer_bootstrap_failed" \
            "bootstrap" \
            "安装暂时没有完成，请稍后重试。" \
            "retry_and_report_the_structured_error"
    fi
    exit "$exit_code"
}

trap 'unexpected_error "$LINENO"' ERR

usage() {
    cat <<'EOF'
Usage: ./install.sh --initialize [options]

Installs the protected main release into the user's independent runtime.
This command is for end users; source development uses setup.sh --development.

Options:
  --repository URL           Confirmed primary release repository; pass the
                             user's URL here on the first invocation
  --python PATH              Python 3.10+ used to create the runtime environment
  --initialize               Compatibility entry point: install, then run a read-only
                             inspection and stop at the next user interaction boundary
  -h, --help                 Show this help
EOF
}

while (($#)); do
    case "$1" in
        --repository)
            if [[ $# -lt 2 ]]; then
                echo "--repository requires a URL" >&2
                emit_error_json "invalid_arguments" "arguments" "--repository requires a URL." "correct_the_install_arguments"
                exit 2
            fi
            repositories[0]="$2"
            shift 2
            ;;
        --fallback-repository)
            if [[ $# -lt 2 ]]; then
                echo "--fallback-repository requires a URL" >&2
                emit_error_json "invalid_arguments" "arguments" "--fallback-repository requires a URL." "correct_the_install_arguments"
                exit 2
            fi
            repositories+=("$2")
            shift 2
            ;;
        --python)
            if [[ $# -lt 2 ]]; then
                echo "--python requires a path" >&2
                emit_error_json "invalid_arguments" "arguments" "--python requires a path." "correct_the_install_arguments"
                exit 2
            fi
            python_bin="$2"
            shift 2
            ;;
        --initialize)
            do_initialize=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            emit_error_json "invalid_arguments" "arguments" "The installer received an unknown option." "correct_the_install_arguments"
            exit 2
            ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "The independent end-user installer currently supports macOS only." >&2
    emit_error_json "unsupported_platform" "preflight" "The independent end-user installer currently supports macOS only." "run_the_installer_on_macos"
    exit 1
fi

if [[ -n "$python_bin" ]]; then
    python_candidates=("$python_bin")
else
    # Prefer the Desktop/agent host interpreter when it publishes one. The
    # installer still creates an isolated runtime venv; this interpreter is
    # only the bootstrap tool used to run installer.py.
    python_candidates=()
    for host_python in "${WECHAT_DECRYPT_PYTHON:-}" "${CODEX_DESKTOP_PYTHON:-}"; do
        [[ -n "$host_python" ]] && python_candidates+=("$host_python")
    done
    # Desktop applications commonly bundle Python below Contents/Resources.
    # Search only the two user-controlled Applications roots, avoiding a
    # repository clone or a system-wide recursive scan.
    for app_root in /Applications "$HOME/Applications"; do
        [[ -d "$app_root" ]] || continue
        for embedded in \
            "$app_root"/*/Contents/Resources/python/python/bin/python3 \
            "$app_root"/*/Contents/Resources/python/bin/python3 \
            "$app_root"/*/Contents/Resources/venv/bin/python3; do
            [[ -x "$embedded" ]] && python_candidates+=("$embedded")
        done
    done
    python_candidates+=(python3.13 python3.12 python3.11 python3.10 python3)
fi
python_bin=""
for candidate in "${python_candidates[@]}"; do
    if { [[ -x "$candidate" ]] || command -v "$candidate" >/dev/null 2>&1; } && \
        "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1; then
        python_bin="$candidate"
        break
    fi
done
if [[ -z "$python_bin" ]]; then
    echo "Python 3.10 or newer was not found in PATH or supported Desktop application locations." >&2
    emit_error_json "python_not_found" "preflight" "Python 3.10 or newer was not found in PATH or supported Desktop application locations." "install_python_3_10_or_newer_or_set_wechat_decrypt_python_and_retry"
    exit 1
fi

normalize_json_output() {
    "$python_bin" -c '
import json
import sys

for line in reversed(sys.argv[1].splitlines()):
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(0)
raise SystemExit(1)
' "$1"
}

existing_install_matches_repository() {
    [[ -x "$MANAGEMENT_CLI" && -f "$INSTALL_MANIFEST" ]] || return 1
    "$python_bin" -c '
import json
import os
import re
import sys
from urllib.parse import urlparse

try:
    with open(sys.argv[1], encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
except (OSError, ValueError, TypeError):
    raise SystemExit(1)

def identity(value):
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    if re.match(r"^[^/@:]+@[^/:]+:", text):
        user_host, path = text.split(":", 1)
        host = user_host.split("@", 1)[1].lower()
        return host, path.removesuffix(".git").strip("/")
    parsed = urlparse(text)
    if parsed.scheme and parsed.hostname:
        return parsed.hostname.lower(), parsed.path.removesuffix(".git").strip("/")
    return "local", os.path.realpath(os.path.expanduser(text))

requested = identity(sys.argv[2])
known = [manifest.get("repository"), manifest.get("source_repository")]
known.extend(manifest.get("repositories") or [])
raise SystemExit(0 if requested in {identity(value) for value in known} else 1)
' "$INSTALL_MANIFEST" "${repositories[0]}"
}

installed_release_is_current() {
    local update_output
    if ! update_output=$("$MANAGEMENT_CLI" --json check-update); then
        # An unavailable update check must not turn a healthy reinstall into a
        # needless download. The installed runtime remains the safe fallback.
        return 0
    fi
    if ! update_output=$(normalize_json_output "$update_output"); then
        return 0
    fi
    "$python_bin" -c '
import json
import sys

payload = json.loads(sys.argv[1])
raise SystemExit(
    1
    if payload.get("command") == "check-update"
    and payload.get("update_available") is True
    else 0
)
' "$update_output"
}

emit_existing_install_result() {
    local inspect_output="$1"
    "$python_bin" -c '
import json
import sys

inspect_data = json.loads(sys.argv[1])
combined = {
    "ok": inspect_data.get("ok", False),
    "command": "install+inspect",
    "phase": "existing_install_inspected" if inspect_data.get("ok") else "inspect",
    "install_complete": True,
    "initialize_complete": bool(inspect_data.get("initialized")),
    "installation_mode": "reused",
    "installation_reused": True,
    "query_ready": inspect_data.get("query_ready", False),
    "endpoint": inspect_data.get("endpoint"),
    "inspect": inspect_data,
}
if not inspect_data.get("ok", False):
    for key in ("error_code", "error", "user_message", "requires_user_action",
                "retry_command", "next_action", "details"):
        if key in inspect_data:
            combined[key] = inspect_data[key]
    combined.setdefault("user_message", "安装暂时没有完成，请稍后重试。")
combined["next_step"] = inspect_data.get(
    "next_step", inspect_data.get("next_action", "review_inspect_error")
)
print(json.dumps(combined, ensure_ascii=False, separators=(",", ":")))
' "$inspect_output"
}

install_source=""
# Keep one empty sentinel for macOS Bash 3.2 + set -u compatibility.
temporary_sources=("")

cleanup() {
    local path
    for path in "${temporary_sources[@]}"; do
        [[ -n "$path" && -d "$path" ]] && rm -rf -- "$path"
    done
    return 0
}
trap cleanup EXIT

# Repeated installation is idempotent. Reuse a healthy installation from the
# same requested source and continue from its current interaction boundary.
if [[ "$do_initialize" == true ]] \
    && existing_install_matches_repository \
    && installed_release_is_current; then
    echo "[检查] 正在确认现有安装..." >&2
    if existing_output=$("$MANAGEMENT_CLI" --json inspect); then
        existing_exit=0
    else
        existing_exit=$?
    fi
    if normalized_existing=$(normalize_json_output "$existing_output"); then
        emit_existing_install_result "$normalized_existing"
        result_emitted=true
        exit "$existing_exit"
    fi
    echo "[检查] 现有安装需要修复，正在继续安装..." >&2
fi

# 代理检测：环境变量未设置时尝试读取 macOS 系统代理
if [[ -z "${https_proxy:-}" && -z "${HTTPS_PROXY:-}" ]]; then
    sys_proxy=$(/usr/sbin/scutil --proxy 2>/dev/null | awk '
        /HTTPSEnable.*1/ { enabled=1 }
        /HTTPSProxy/ { proxy=$NF }
        /HTTPSPort/ { port=$NF }
        END { if (enabled && proxy && port) print "http://" proxy ":" port }
    ')
    if [[ -n "$sys_proxy" ]]; then
        export https_proxy="$sys_proxy"
        export http_proxy="$sys_proxy"
        echo "[准备] 已使用本机网络设置" >&2
    fi
fi

echo "[准备] 正在下载安装文件..." >&2
for repository in "${repositories[@]}"; do
    for attempt in 1 2 3; do
        candidate="$(mktemp -d "${TMPDIR:-/tmp}/wechat-decrypt-light.XXXXXX")"
        temporary_sources+=("$candidate")
        if /usr/bin/git -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=15 \
            clone --quiet --depth 1 --no-tags --branch "$RELEASE_BRANCH" --single-branch \
            "$repository" "$candidate"; then
            install_source="$candidate"
            source_repository="$repository"
            break 2
        fi
        [[ $attempt -lt 3 ]] && sleep 1
    done
done

if [[ -z "$install_source" ]]; then
    echo "[网络] 暂时无法下载安装文件" >&2
    emit_error_json "all_git_sources_unreachable" "download" "暂时无法下载安装文件，请检查网络后重试。" "retry_network_or_add_an_official_fallback_repository"
    exit 1
fi

install_args=(
    install
    --json
    --source "$install_source"
    --repository "${repositories[0]}"
    --branch "$RELEASE_BRANCH"
    --python "$python_bin"
)
for repository in "${repositories[@]:1}"; do
    install_args+=(--fallback-repository "$repository")
done

echo "[安装] 正在完成安装，首次使用可能需要一些时间..." >&2
if install_output=$("$python_bin" "$install_source/installer.py" "${install_args[@]}"); then
    install_exit=0
else
    install_exit=$?
fi

if [[ $install_exit -ne 0 ]]; then
    if normalized_install=$(normalize_json_output "$install_output"); then
        printf '%s\n' "$normalized_install"
        result_emitted=true
    else
        emit_error_json "install_output_invalid" "install" "安装暂时没有完成，请稍后重试。" "retry_and_report_the_structured_error"
    fi
    exit $install_exit
fi

if normalized_install=$(normalize_json_output "$install_output"); then
    install_output="$normalized_install"
else
    emit_error_json "install_output_invalid" "install" "安装暂时没有完成，请稍后重试。" "retry_and_report_the_structured_error"
    exit 1
fi

# If --initialize not requested, output install result and exit
if [[ "$do_initialize" != true ]]; then
    printf '%s\n' "$install_output"
    result_emitted=true
    exit 0
fi

# Compatibility behavior: inspect only. Sensitive initialization must be a
# separate Agent-driven stage after the structured result identifies the next
# user interaction boundary.
echo "[检查] 正在确认下一步..." >&2
if inspect_output=$("$MANAGEMENT_CLI" --json inspect); then
    inspect_exit=0
else
    inspect_exit=$?
fi

if normalized_inspect=$(normalize_json_output "$inspect_output"); then
    inspect_output="$normalized_inspect"
else
    inspect_output='{"ok":false,"command":"inspect","error_code":"inspect_output_invalid","error":"管理程序返回了无效结果。","user_message":"安装暂时没有完成，请稍后重试。","next_action":"retry_inspect_and_report_the_structured_error"}'
    inspect_exit=1
fi

# Build one structured result without crossing a sensitive-operation boundary.
"$python_bin" -c "
import json, sys

install_data = json.loads(sys.argv[1])
inspect_data = json.loads(sys.argv[2])

combined = {
    'ok': install_data.get('ok', False) and inspect_data.get('ok', False),
    'command': 'install+inspect',
    'phase': 'inspection_complete' if inspect_data.get('ok', False) else 'inspect',
    'install_complete': install_data.get('ok', False),
    'initialize_complete': False,
    'install': install_data,
    'inspect': inspect_data,
    'authorization_prompt_count': 0,
    'query_ready': inspect_data.get('query_ready', False),
    'installation_mode': install_data.get('installation_mode', 'fresh'),
    'installation_reused': install_data.get('installation_reused', False),
    'endpoint': inspect_data.get('endpoint') or install_data.get('installation', {}).get('endpoint'),
}
if not inspect_data.get('ok', False):
    for key in ('error_code', 'error', 'user_message', 'requires_user_action',
                'retry_command', 'next_action', 'details'):
        if key in inspect_data:
            combined[key] = inspect_data[key]
    combined.setdefault('user_message', '安装暂时没有完成，请稍后重试。')
combined['next_step'] = inspect_data.get(
    'next_step', inspect_data.get('next_action', 'review_inspect_error')
)

print(json.dumps(combined, ensure_ascii=False))
" "$install_output" "$inspect_output"
result_emitted=true

# Exit with the worst of the two exit codes.
[[ $inspect_exit -ne 0 ]] && exit $inspect_exit
exit 0
