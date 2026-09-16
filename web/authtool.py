#!/usr/bin/env python3
"""
safescanarr/web/authtool.py

CLI escape hatch for Safe Scanarr authentication.

Usage:
    python3 -m web.authtool status
    python3 -m web.authtool reset
    python3 -m web.authtool set-password
    python3 -m web.authtool set-token [--show]
"""

import argparse
import getpass
import os
import sys

# Ensure project root is importable
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import config as config_module
from web import auth


def _config_path() -> str:
    return config_module._config_path()


def _warn_owner():
    path = _config_path()
    try:
        st = os.stat(path)
        if st.st_uid != os.geteuid():
            print(f"WARNING: config file is owned by uid {st.st_uid}, but you are uid {os.geteuid()}. "
                  "Run this command as the service user to avoid permission problems.", file=sys.stderr)
    except FileNotFoundError:
        pass


def cmd_status(_args):
    cfg = config_module.raw()
    method = cfg.get("auth_method") or ("token" if cfg.get("auth_token") else "")
    print(f"auth_method: {method or 'unconfigured'}")
    print(f"config file: {_config_path()}")
    print(f"token configured: {'yes' if cfg.get('auth_token') else 'no'}")
    print(f"password configured: {'yes' if cfg.get('auth_password_hash') else 'no'}")


def cmd_reset(_args):
    auth.reset()
    print("Authentication reset. Restart the server and visit /setup to configure again.")


def cmd_set_password(_args):
    pw = getpass.getpass("Enter new password (min 10 chars): ")
    confirm = getpass.getpass("Confirm password: ")
    try:
        auth.configure_password(pw, confirm)
        print("Password set. Existing sessions are invalidated.")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_set_token(args):
    token = config_module.raw().get("auth_token")
    if not token:
        token = auth.configure_token()
        print("Generated new token.")
    else:
        print("Using existing token.")
    if args.show:
        print(token)
    else:
        print("Use --show to display the token.")


def main():
    parser = argparse.ArgumentParser(prog="authtool", description="Safe Scanarr auth CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current auth status")
    sub.add_parser("reset", help="Reset auth to first-run state")
    sub.add_parser("set-password", help="Set password auth")

    p_token = sub.add_parser("set-token", help="Set or reveal token auth")
    p_token.add_argument("--show", action="store_true", help="Print the token")

    args = parser.parse_args()
    _warn_owner()

    handlers = {
        "status": cmd_status,
        "reset": cmd_reset,
        "set-password": cmd_set_password,
        "set-token": cmd_set_token,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
