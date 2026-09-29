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
    user_header: str = "X-User"
    bootstrap_admins: list[str] = field(default_factory=list)

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
            user_header=env("USER_HEADER", "X-User"),
            bootstrap_admins=_csv(env("BOOTSTRAP_ADMINS")),
            lock_ttl_seconds=int(env("LOCK_TTL_SECONDS", "600")),
            audit_query_limit=int(env("AUDIT_QUERY_LIMIT", "500")),
        )

    def validate(self) -> None:
        if self.storage_backend not in ("s3", "local"):
            raise ValueError("STORAGE_BACKEND must be 's3' or 'local'")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
