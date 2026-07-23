#!/usr/bin/env bash
# Canonical bootstrap for an end-user macOS installation.
set -euo pipefail

readonly DEFAULT_REPOSITORY="https://github.com/aifeidisheng/local-msg-cli.git"
readonly RELEASE_BRANCH="main"

repositories=("${WECHAT_DECRYPT_REPOSITORY:-$DEFAULT_REPOSITORY}")
python_bin="${WECHAT_DECRYPT_PYTHON:-}"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Installs the protected main release into the user's independent runtime.
This command is for end users; source development uses setup.sh --development.

Options:
  --repository URL           Confirmed primary release repository
  --fallback-repository URL  Confirmed fallback repository (repeatable)
  --python PATH              Python 3.10+ used to create the runtime environment
  -h, --help                 Show this help
EOF
}

while (($#)); do
    case "$1" in
        --repository)
            [[ $# -ge 2 ]] || { echo "--repository requires a URL" >&2; exit 2; }
            repositories[0]="$2"
            shift 2
            ;;
        --fallback-repository)
            [[ $# -ge 2 ]] || { echo "--fallback-repository requires a URL" >&2; exit 2; }
            repositories+=("$2")
            shift 2
            ;;
        --python)
            [[ $# -ge 2 ]] || { echo "--python requires a path" >&2; exit 2; }
            python_bin="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "The independent end-user installer currently supports macOS only." >&2
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
    exit 1
fi

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
"$python_bin" "$install_source/installer.py" "${install_args[@]}"
