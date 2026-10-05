"""Admin command line, run inside the container:

    docker compose exec es-config-api python -m app.cli reset-password someone@fenixcommerce.com
        Prints a new generated password (ends the user's sessions). Use it if the only admin
        is locked out or has forgotten their password.

    docker compose exec -it es-config-api python -m app.cli set-cluster-secret elkm2-prod
        Asks for the Elasticsearch password (or --api-key) of a cluster and stores it in
        Secrets Manager as <SECRETS_PREFIX>clusters/<id>. Then remove the password line from
        clusters.yaml and its variable from .env.

    docker compose exec es-config-api python -m app.cli secrets-status
        Lists which secrets exist and the region each is read from (names only, never values).

    docker compose exec es-config-api python -m app.cli migrate-secrets --dry-run
        With SECRETS_LEGACY_REGION set: lists the secrets still in the legacy region. Without
        --dry-run it copies each one (value unchanged) to the main region and schedules the
        legacy copy for deletion (recoverable for 7 days). Safe to run again.
"""
from __future__ import annotations

import getpass
import sys

from .auth import generate_password, hash_password, normalize_username
from .clusters import load_clusters
from .repos import AuditRepo, UsersRepo
from .secret_store import build_secret_store
from .settings import Settings
from .storage import build_store


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    secrets = build_secret_store(settings)
    cmd, args = argv[0], argv[1:]

    if cmd == "reset-password" and len(args) == 1:
        store = build_store(settings)
        users = UsersRepo(store, settings.bootstrap_admins, secrets)
        name = normalize_username(args[0])
        if users.get(name) is None:
            print(f"Unknown user '{name}'", file=sys.stderr)
            return 1
        password = generate_password()
        users.set_password(name, hash_password(password), True, "cli")
        AuditRepo(store).write({"action": "ADMIN_PASSWORD_RESET", "actor": "cli",
                                "outcome": "SUCCESS", "targetUser": name})
        print(f"username,password\n{name},{password}")
        return 0

    if cmd == "set-cluster-secret" and args:
        cid, api_key = args[0], "--api-key" in args
        prompt = f"{'API key' if api_key else 'Password'} for cluster {cid}: "
        value = getpass.getpass(prompt).strip()
        if not value:
            print("Nothing entered; nothing changed", file=sys.stderr)
            return 1
        secrets.put(f"clusters/{cid}", {"apiKey" if api_key else "password": value},
                    f"ES Config API: credentials for cluster {cid}")
        print(f"Stored in {secrets.full_name('clusters/' + cid)}. Restart is not needed; the API "
              "picks it up within 5 minutes (or run: docker compose restart).")
        return 0

    if cmd == "secrets-status":
        for n in _secret_names(settings):
            where = secrets.where(n)
            print(f"{'present' if where else 'MISSING':8}  {where or '-':10}  {secrets.full_name(n)}")
        return 0

    if cmd == "migrate-secrets":
        dry = "--dry-run" in args
        legacy = getattr(secrets, "legacy_region", None)
        if not legacy:
            print("SECRETS_LEGACY_REGION is not set (or equals the main region): nothing to move")
            return 0
        print(f"{'Would move' if dry else 'Moving'} secrets from {legacy} to {secrets.region}"
              f"{' (dry run, nothing changed)' if dry else ''}")
        counts: dict[str, int] = {}
        for n in _secret_names(settings):
            what = secrets.migrate(n, dry_run=dry)
            counts[what] = counts.get(what, 0) + 1
            label = {"moved": "would move" if dry else "moved",
                     "retired": "old copy would be retired" if dry else "old copy retired",
                     "in-main": "already moved", "missing": "MISSING"}[what]
            print(f"{label:26}  {secrets.full_name(n)}")
        print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        if not dry and (counts.get("moved") or counts.get("retired")):
            print(f"The {legacy} copies are scheduled for deletion in 7 days (restore them in the "
                  "AWS console if needed). Restart is not needed.")
        return 0

    print(__doc__)
    return 2


def _secret_names(settings: Settings) -> list[str]:
    """Every secret the API uses: app, each cluster that has credentials, each user."""
    from .clusters import ClusterRegistry
    reg = ClusterRegistry(load_clusters(settings.clusters_file), settings.managed_clusters_file or None)
    # clusters without authentication (auth type none) have no secret
    names = ["app"] + sorted({c.secret for c in reg.all() if c.secret})
    store = build_store(settings)
    names += [f"users/{u['username']}" for u in UsersRepo(store, settings.bootstrap_admins).list()]
    return names


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
