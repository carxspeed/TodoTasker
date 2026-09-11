"""Safely manage TodoTasker's Windows-encrypted credential vault."""

from __future__ import annotations

import argparse
import getpass

from daily_brief.secret_vault import SECRET_NAMES, SecretVault, SecretVaultError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set", help="store a secret using a hidden prompt")
    set_command.add_argument("name", choices=sorted(SECRET_NAMES))
    delete_command = commands.add_parser("delete", help="remove one stored secret")
    delete_command.add_argument("name", choices=sorted(SECRET_NAMES))
    commands.add_parser("status", help="show which secrets exist without revealing values")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        vault = SecretVault()
        if args.command == "set":
            value = getpass.getpass(f"Enter {args.name}: ")
            vault.set(args.name, value)
            print(f"Stored {args.name} in the Windows-encrypted vault.")
        elif args.command == "delete":
            removed = vault.delete(args.name)
            print(f"{'Removed' if removed else 'Not configured'}: {args.name}")
        else:
            configured = set(vault.configured())
            for name in sorted(SECRET_NAMES):
                print(f"{name}: {'configured' if name in configured else 'not configured'}")
        return 0
    except SecretVaultError as exc:
        print(f"SECRET_VAULT_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
