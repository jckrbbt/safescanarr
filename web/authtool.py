#!/usr/bin/env python3
"""
safescanarr/web/authtool.py

CLI escape hatch for Safe Scanarr authentication.

Usage:
    python3 -m web.authtool status
    python3 -m web.authtool reset
    python3 -m web.authtool set-password
    python3 -m web.authtool set-token [--show]
    python3 -m web.authtool revoke-sessions
"""

import argparse
import errno
import getpass
import os
import stat
import sys

# Ensure project root is importable
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import config as config_module
import config_lock
from config import Config
from database import Database
from web import auth


def _purge_web_sessions() -> None:
    """Revocation hook: credential transitions also drop remembered rows."""
    db = Database(Config().DB_FILE)
    try:
        db.delete_web_sessions_except(None)
    finally:
        db.close()


auth.register_revocation_hook(_purge_web_sessions)


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
        print("Existing sessions are invalidated.")
    else:
        print("Using existing token.")
    if args.show:
        print(token)
    else:
        print("Use --show to display the token.")


def cmd_revoke_sessions(_args):
    auth.revoke_all_sessions()
    print("All web sessions (including remembered devices) have been signed out.")


def _remove_if_same_inode(path: str, fd: int) -> bool:
    """Unlink *path* only if it still names the inode behind *fd*.

    Compares the inode TWICE — once by path, then again through a
    directory fd — so a replacement landing BEFORE the last comparison
    is detected and refused instead of removed. This narrows (not
    eliminates) the stat→unlink window: a fully uncoordinated process
    that replaces the lock between the second stat and the unlink is
    outside the supported model, and no atomic unlink-if-inode promise
    is made. Repairs are a documented maintenance operation: run them
    with other writers stopped and never two at once.
    """
    import config_lock
    try:
        if os.stat(path).st_ino != os.fstat(fd).st_ino:
            return False  # path was replaced: not ours to remove
    except OSError:
        return False      # already gone
    parent = os.path.dirname(path) or "."
    name = os.path.basename(path)
    dirfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            st = os.stat(name, dir_fd=dirfd)
        except OSError:
            return False  # gone between our first stat and now
        if st.st_ino != os.fstat(fd).st_ino:
            return False  # replaced while we opened the directory
        try:
            os.unlink(name, dir_fd=dirfd)
        except FileNotFoundError:
            return False  # somebody else removed it first
        return True
    finally:
        os.close(dirfd)


def cmd_fix_lock(_args):
    """Operator-controlled repair of the config lock file.

    Removes a lock file that fails validation (wrong owner/mode/symlink)
    and recreates it fresh with mode 0600, revalidating the inode identity
    (by path, then again through a directory fd) so a replacement detected
    BEFORE the last comparison is refused; the residual window between the
    last stat and the unlink is covered only by the maintenance model
    below, not by an atomic unlink-if-inode operation.

    MAINTENANCE OPERATION: run it with the web service and any writers
    (authtool, poller) STOPPED. A mode-invalid lock may still be held by a
    writer that validated it before the mode changed; replacing it under a
    live writer is not supported and is refused where detectable. Lock
    validation itself is never silently weakened.
    """
    import config_lock
    path = config_module._config_path() + ".lock"
    # Open the lock WITHOUT validation to inspect the actual inode: an
    # invalid-mode file cannot go through _FileLock, so hold its fd for
    # identity revalidation instead of trusting the pathname.
    import fcntl
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        probe = os.open(path, flags, 0o600)
    except OSError as e:
        if getattr(e, "errno", None) in (errno.ELOOP, errno.EMLINK) or (
                hasattr(os, "O_NOFOLLOW") and e.errno == errno.EPERM):
            print(f"Lock file {path} is a symlink; removing it...")
            probe = None
        else:
            print(f"Error: could not access the lock file: {e}", file=sys.stderr)
            sys.exit(1)
    # Fast path: is the current lock valid AND quiescent? Nothing to do.
    if probe is not None:
        try:
            config_lock._validate_lock_fd(probe, path)
        except config_lock.LockValidationError as e:
            print(f"Lock file {path} is invalid: {e}")
            try:
                held = fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("Error: the invalid lock file is actively held by "
                      "another process. Stop the web service and all "
                      "writers before repairing.", file=sys.stderr)
                os.close(probe)
                sys.exit(1)
            if not _remove_if_same_inode(path, probe):
                print("Lock file was replaced by a concurrent repair; "
                      "nothing to remove.", file=sys.stderr)
                os.close(probe)
                sys.exit(1)
            os.close(probe)
            print("Removed the invalid lock file; recreating with mode 0600...")
        else:
            # Valid inode: a valid lock is never a repair target. Still
            # wait for any active holder (matches the historical
            # behaviour: a solitary repair blocks on a held valid lock)
            # and revalidate under the lock before declaring it healthy.
            os.close(probe)
            try:
                with config_lock._FileLock():
                    pass
            except (config_lock.LockValidationError, OSError) as e:
                print(f"Error: lock changed under repair: {e}",
                      file=sys.stderr)
                sys.exit(1)
            print("Lock file is already valid; nothing to do.")
            return
    else:
        # Symlink case: identity check via lstat not applicable — the
        # symlink itself is the invalid object; remove exactly that.
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                os.unlink(path)
                print("Removed the symlinked lock file; recreating with mode 0600...")
        except FileNotFoundError:
            pass
    try:
        with config_lock._FileLock():
            pass
    except (config_lock.LockValidationError, OSError) as e:
        print(f"Error: could not recreate a valid lock file: {e}",
              file=sys.stderr)
        sys.exit(1)
    print(f"Lock file {path} recreated (mode 0600).")


def main():
    parser = argparse.ArgumentParser(prog="authtool", description="Safe Scanarr auth CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current auth status")
    sub.add_parser("reset", help="Reset auth to first-run state")
    sub.add_parser("set-password", help="Set password auth")

    p_token = sub.add_parser("set-token", help="Set or reveal token auth")
    p_token.add_argument("--show", action="store_true", help="Print the token")

    sub.add_parser("revoke-sessions", help="Sign out all web sessions on every device")
    sub.add_parser("fix-lock", help="Repair an invalid config.json.lock (removes and recreates it, mode 0600; "
                                    "maintenance operation — stop the web service and other writers first)")

    args = parser.parse_args()
    _warn_owner()

    handlers = {
        "status": cmd_status,
        "reset": cmd_reset,
        "set-password": cmd_set_password,
        "set-token": cmd_set_token,
        "revoke-sessions": cmd_revoke_sessions,
        "fix-lock": cmd_fix_lock,
    }
    try:
        handlers[args.command](args)
    except config_lock.LockValidationError as e:
        print(f"Error: configuration change failed — the config lock file is "
              f"unusable ({e}). Fix the ownership/mode of config.json.lock "
              f"in BASE_DIR and retry.", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error: filesystem failure — {e}. Check that BASE_DIR is "
              f"writable by the service user.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
