"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    # Storage
    storage_backend: str = "s3"               # "s3" or "local" (local = dev only)
    s3_bucket: str = ""
    s3_prefix: str = "es-config-api/"
    aws_region: str | None = None
    s3_endpoint_url: str | None = None        # only for S3-compatible test stores
    s3_sse: str | None = None                 # "aws:kms" / "AES256"; unset = bucket default
    local_store_dir: str = "./.local-store"

    # Clusters
    clusters_file: str = "/app/config/clusters.yaml"

    # Identity / permissions
    auth_mode: str = "password"               # "password" (production) | "header" (dev/tests only)
    user_header: str = "X-User"               # header mode only
    bootstrap_admins: list[str] = field(default_factory=list)
    bootstrap_admin_password: str | None = None
    session_secret: str = ""
    session_hours: float = 12.0
    cookie_secure: bool = False               # set true once the API is served over HTTPS
    lockout_attempts: int = 5
    lockout_minutes: int = 15

    # Behaviour
    lock_ttl_seconds: int = 600
    audit_query_limit: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        prefix = env("S3_PREFIX", "es-config-api/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return cls(
            storage_backend=env("STORAGE_BACKEND", "s3").lower(),
            s3_bucket=env("S3_BUCKET", ""),
            s3_prefix=prefix,
            aws_region=env("AWS_REGION") or env("AWS_DEFAULT_REGION"),
            s3_endpoint_url=env("S3_ENDPOINT_URL") or None,
            s3_sse=env("S3_SSE") or None,
            local_store_dir=env("LOCAL_STORE_DIR", "./.local-store"),
            clusters_file=env("CLUSTERS_FILE", "/app/config/clusters.yaml"),
            auth_mode=env("AUTH_MODE", "password").lower(),
            user_header=env("USER_HEADER", "X-User"),
            bootstrap_admins=[a.lower() for a in _csv(env("BOOTSTRAP_ADMINS"))],
            bootstrap_admin_password=env("BOOTSTRAP_ADMIN_PASSWORD") or None,
            session_secret=env("SESSION_SECRET", ""),
            session_hours=float(env("SESSION_HOURS", "12")),
            cookie_secure=env("COOKIE_SECURE", "false").lower() in ("1", "true", "yes"),
            lockout_attempts=int(env("LOCKOUT_ATTEMPTS", "5")),
            lockout_minutes=int(env("LOCKOUT_MINUTES", "15")),
            lock_ttl_seconds=int(env("LOCK_TTL_SECONDS", "600")),
            audit_query_limit=int(env("AUDIT_QUERY_LIMIT", "500")),
        )

    def validate(self) -> None:
        if self.storage_backend not in ("s3", "local"):
            raise ValueError("STORAGE_BACKEND must be 's3' or 'local'")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        if self.auth_mode not in ("password", "header"):
            raise ValueError("AUTH_MODE must be 'password' or 'header'")
        if self.auth_mode == "password" and len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must be set to a random string of 32+ characters "
                             "(e.g. `openssl rand -hex 32`)")
