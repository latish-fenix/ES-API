"""Admin command line, run inside the container:

    docker compose exec es-config-api python -m app.cli reset-password someone@fenixcommerce.com

Prints a new generated password (ends the user's sessions). Use it if the only admin
is locked out or has forgotten their password.
"""
from __future__ import annotations

import sys

from .auth import generate_password, hash_password, normalize_username
from .repos import AuditRepo, UsersRepo
from .settings import Settings
from .storage import build_store


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "reset-password":
        print(__doc__)
        return 2
    settings = Settings.from_env()
    store = build_store(settings)
    users = UsersRepo(store, settings.bootstrap_admins)
    name = normalize_username(argv[1])
    if users.get(name) is None:
        print(f"Unknown user '{name}'", file=sys.stderr)
        return 1
    password = generate_password()
    users.set_password(name, hash_password(password), True, "cli")
    AuditRepo(store).write({"action": "ADMIN_PASSWORD_RESET", "actor": "cli", "outcome": "SUCCESS",
                            "targetUser": name})
    print(f"username,password\n{name},{password}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
