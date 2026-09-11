"""Safely manage TodoTasker's Windows-encrypted credential vault."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from dotenv import dotenv_values

from daily_brief.envfile import blank_env_values
from daily_brief.secret_vault import SECRET_NAMES, SecretVault, SecretVaultError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set", help="store a secret using a hidden prompt")
    set_command.add_argument("name", choices=sorted(SECRET_NAMES))
    delete_command = commands.add_parser("delete", help="remove one stored secret")
    delete_command.add_argument("name", choices=sorted(SECRET_NAMES))
    commands.add_parser("status", help="show which secrets exist without revealing values")
    commands.add_parser(
        "migrate-env",
        help="encrypt current .env secrets, verify them, then blank the plaintext values",
    )
    return parser.parse_args()


def migrate_env_secrets(path: Path, vault: SecretVault) -> tuple[str, ...]:
    file_values = dotenv_values(path)
    updates = {
        name: str(file_values[name])
        for name in SECRET_NAMES
        if file_values.get(name) is not None and str(file_values[name])
    }
    if not updates:
        return ()
    vault.set_many(updates)
    verified = vault.get_many(frozenset(updates))
    if verified != updates:
        raise SecretVaultError("vault verification failed; .env was not changed")
    blank_env_values(path, SECRET_NAMES)
    remaining = dotenv_values(path)
    if any(str(remaining.get(name, "")).strip() for name in SECRET_NAMES):
        raise SecretVaultError(".env cleanup could not be verified")
    return tuple(sorted(updates))


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
        elif args.command == "migrate-env":
            migrated = migrate_env_secrets(args.env_file, vault)
            if migrated:
                print("Encrypted and removed from .env: " + ", ".join(migrated))
            else:
                print("No plaintext secrets were found in .env.")
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
