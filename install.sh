#!/usr/bin/env bash
# Canonical bootstrap for an end-user macOS installation.
# This script is SELF-CONTAINED — it does not reference any other file from
# the repository. It can be downloaded standalone (e.g. via curl) and executed
# directly; it will clone the verified main release internally.
set -euo pipefail

readonly DEFAULT_REPOSITORY="https://github.com/aifeidisheng/local-msg-cli.git"
readonly RELEASE_BRANCH="main"
readonly MANAGEMENT_CLI="$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light"

repositories=("${WECHAT_DECRYPT_REPOSITORY:-$DEFAULT_REPOSITORY}")
python_bin="${WECHAT_DECRYPT_PYTHON:-}"
do_initialize=false
result_emitted=false

command_name() {
    if [[ "$do_initialize" == true ]]; then
        printf '%s' 'install+initialize'
    else
        printf '%s' 'install'
    fi
}

emit_error_json() {
    local error_code="$1"
    local phase="$2"
    local error_message="$3"
    local next_action="$4"
    result_emitted=true
    printf '{"ok":false,"command":"%s","phase":"%s","error_code":"%s","error":"%s","next_action":"%s"}\n' \
        "$(command_name)" "$phase" "$error_code" "$error_message" "$next_action"
}

unexpected_error() {
    local exit_code=$?
    local line_number="$1"
    if [[ "$result_emitted" != true ]]; then
        echo "Unexpected installer bootstrap failure at line $line_number (exit $exit_code)." >&2
        emit_error_json \
            "installer_bootstrap_failed" \
            "bootstrap" \
            "The installer bootstrap failed before producing a result." \
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
  --repository URL           Confirmed primary release repository
  --fallback-repository URL  Confirmed fallback repository (repeatable)
  --python PATH              Python 3.10+ used to create the runtime environment
  --initialize               Canonical end-user flow: install and immediately initialize
                             (extracts DB keys via macOS admin prompt, then
                             pre-decrypts databases so MCP is query-ready)
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
    python_candidates=(python3.13 python3.12 python3.11 python3.10 python3)
fi
python_bin=""
for candidate in "${python_candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 && \
        "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1; then
        python_bin="$candidate"
        break
    fi
done
if [[ -z "$python_bin" ]]; then
    echo "Python 3.10 or newer was not found. Use --python PATH to select one." >&2
    emit_error_json "python_not_found" "preflight" "Python 3.10 or newer was not found." "install_python_3_10_or_newer_and_retry"
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

install_source=""
temporary_sources=()

cleanup() {
    local path
    for path in "${temporary_sources[@]}"; do
        [[ -n "$path" && -d "$path" ]] && rm -rf -- "$path"
    done
}
trap cleanup EXIT

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
        echo "[proxy] 检测到系统 HTTPS 代理: $sys_proxy" >&2
    fi
fi

for repository in "${repositories[@]}"; do
    for attempt in 1 2 3; do
        candidate="$(mktemp -d "${TMPDIR:-/tmp}/wechat-decrypt-light.XXXXXX")"
        temporary_sources+=("$candidate")
        echo "[source] Cloning confirmed main release from $repository (attempt $attempt/3)" >&2
        if /usr/bin/git -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=15 \
            clone --depth 1 --branch "$RELEASE_BRANCH" --single-branch \
            "$repository" "$candidate"; then
            install_source="$candidate"
            source_repository="$repository"
            break 2
        fi
        [[ $attempt -lt 3 ]] && sleep 1
    done
done

if [[ -z "$install_source" ]]; then
    echo "All confirmed release repositories are unreachable." >&2
    if [[ -z "${https_proxy:-}" ]]; then
        echo "提示: 如使用 Clash/V2Ray 等代理工具，请设置:" >&2
        echo "  export https_proxy=http://127.0.0.1:<端口>" >&2
        echo "  export http_proxy=http://127.0.0.1:<端口>" >&2
    fi
    emit_error_json "all_git_sources_unreachable" "download" "All confirmed release repositories are unreachable." "retry_network_or_add_an_official_fallback_repository"
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

echo "[install] Deploying verified commit from $source_repository" >&2
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
        emit_error_json "install_output_invalid" "install" "The release installer failed without a structured result." "retry_and_report_the_structured_error"
    fi
    exit $install_exit
fi

if normalized_install=$(normalize_json_output "$install_output"); then
    install_output="$normalized_install"
else
    emit_error_json "install_output_invalid" "install" "The release installer returned an invalid result." "retry_and_report_the_structured_error"
    exit 1
fi

# If --initialize not requested, output install result and exit
if [[ "$do_initialize" != true ]]; then
    printf '%s\n' "$install_output"
    result_emitted=true
    exit 0
fi

# Chain initialize: extract keys + pre-decrypt databases
echo "[initialize] Running initialization (macOS admin prompt will appear)..." >&2
if init_output=$("$MANAGEMENT_CLI" --json initialize); then
    init_exit=0
else
    init_exit=$?
fi

if normalized_init=$(normalize_json_output "$init_output"); then
    init_output="$normalized_init"
else
    init_output='{"ok":false,"command":"initialize","error_code":"initialize_output_invalid","error":"The management CLI returned an invalid result.","next_action":"retry_initialize_and_report_the_structured_error"}'
    init_exit=1
fi

# Build combined JSON output: merge install and initialize results
"$python_bin" -c "
import json, sys

install_data = json.loads(sys.argv[1])
init_data = json.loads(sys.argv[2])

combined = {
    'ok': install_data.get('ok', False) and init_data.get('ok', False),
    'command': 'install+initialize',
    'phase': 'complete' if init_data.get('ok', False) else 'initialize',
    'install_complete': install_data.get('ok', False),
    'initialize_complete': init_data.get('ok', False),
    'install': install_data,
    'initialize': init_data,
    'authorization_prompt_count': init_data.get('authorization_prompt_count', 0),
    'query_ready': init_data.get('query_ready', False),
    'endpoint': init_data.get('endpoint') or install_data.get('installation', {}).get('endpoint'),
}
if not init_data.get('ok', False):
    for key in ('error_code', 'error', 'next_action', 'details'):
        if key in init_data:
            combined[key] = init_data[key]
if combined['query_ready']:
    combined['next_step'] = 'register_with_mcporter'
elif init_data.get('ok'):
    combined['next_step'] = 'wait_until_query_ready'
else:
    combined['next_step'] = init_data.get('next_action', 'review_initialize_error')

print(json.dumps(combined, ensure_ascii=False))
" "$install_output" "$init_output"
result_emitted=true

# Exit with the worst of the two exit codes
[[ $init_exit -ne 0 ]] && exit $init_exit
exit 0
