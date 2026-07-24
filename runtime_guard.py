#!/usr/bin/env python3
"""Prevent a macOS source checkout from becoming an end-user runtime."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path


DEVELOPMENT_MARKER = ".wechat-decrypt-development"
INSTALLED_RUNTIME_MARKER = ".wechat-decrypt-installed-runtime"
CANONICAL_INSTALL_COMMAND = "./install.sh --initialize"


def execution_mode(root: Path | None = None) -> str | None:
    root = (root or Path(__file__).resolve().parent).resolve()
    if (root / INSTALLED_RUNTIME_MARKER).is_file():
        return "installed_runtime"
    if (root / DEVELOPMENT_MARKER).is_file():
        return "source_development"
    return None


def require_macos_execution_mode(
    command: str,
    *,
    root: Path | None = None,
    system: str | None = None,
) -> None:
    if (system or platform.system()).lower() != "darwin":
        return
    if execution_mode(root) is not None:
        return

    payload = {
        "ok": False,
        "command": command,
        "error_code": "end_user_must_use_installer",
        "error": f"拒绝从未初始化为开发环境的源码工作树执行 {command}",
        "next_action": "run_install_sh_initialize",
        "canonical_command": CANONICAL_INSTALL_COMMAND,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
    raise SystemExit(2)
