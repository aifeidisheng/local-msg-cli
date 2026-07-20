#!/usr/bin/env python3
"""Guarded entry points for actions that modify or inspect WeChat."""

import argparse
import os
import subprocess
import sys

from config import load_config
from wechat_version_guard import enforce_risky_action_or_exit


def _check_pids(pids):
    cfg = load_config()
    enforce_risky_action_or_exit(
        cfg,
        action="读取微信进程内存获取密钥",
        pids=pids,
    )


def _sign_wechat(app_path):
    app_path = os.path.abspath(os.path.expanduser(os.path.expandvars(app_path)))
    cfg = load_config()

    # Check before stopping WeChat and again immediately before codesign.
    enforce_risky_action_or_exit(cfg, action="重签名微信客户端", app_path=app_path)
    subprocess.run(["pkill", "-x", "WeChat"], check=False)
    enforce_risky_action_or_exit(cfg, action="重签名微信客户端", app_path=app_path)

    command = ["codesign", "--force", "--deep", "--sign", "-", app_path]
    if os.geteuid() != 0:
        command.insert(0, "sudo")
    subprocess.run(command, check=True)
    print(f"[+] 微信重签名完成: {app_path}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信风险动作版本门禁")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check-pids", help="校验即将读取的微信进程")
    check_parser.add_argument("pids", nargs="+", type=int)

    sign_parser = subparsers.add_parser("sign-wechat", help="校验版本后重签名微信")
    sign_parser.add_argument("app_path", nargs="?", default="/Applications/WeChat.app")

    args = parser.parse_args(argv)
    if args.command == "check-pids":
        _check_pids(args.pids)
    elif args.command == "sign-wechat":
        _sign_wechat(args.app_path)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"[!] 风险动作执行失败: {exc}", file=sys.stderr, flush=True)
        sys.exit(exc.returncode or 1)
