"""Where every secret lives: AWS Secrets Manager (production) or local files (dev/tests).

Secret names (all under SECRETS_PREFIX, default ``es-config-api/``), each a JSON object:

    es-config-api/app                    {"sessionSecret": "...", "bootstrapAdminPassword": "..."}
    es-config-api/users/<email>          {"passwordHash": "scrypt$..."}   (console sign-in)
    es-config-api/clusters/<clusterId>   {"password": "..."} or {"apiKey": "..."}   (Elasticsearch)

Nothing secret is written to S3, the managed clusters file, .env or the audit log. Reads
are cached for a few minutes, so a value rotated in the AWS console is picked up without
a restart; the API's own writes update the cache at once.

Moving regions: with SECRETS_LEGACY_REGION set (for example us-east-1 while SECRETS_REGION
or AWS_REGION is us-west-2), new secrets are created in the main region, reads fall back to
the legacy region, and a secret moves the first time it is written: the new value goes to
the main region and the legacy copy is scheduled for deletion with a 7-day recovery window.
``python -m app.cli migrate-secrets`` moves the rest in one go.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets as pysecrets
import threading
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .errors import ApiError

log = logging.getLogger("es_config_api.secrets")

_NAME_RE = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")


class SecretStore(Protocol):
    prefix: str
    backend: str

    def get(self, name: str) -> dict | None: ...
    def put(self, name: str, value: dict, description: str = "") -> None: ...
    def delete(self, name: str) -> None: ...
    def full_name(self, name: str) -> str: ...
    def where(self, name: str) -> str | None: ...


def _check(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid secret name {name!r}")
    return name


class _Cache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return True, hit[1]
        return False, None

    def set(self, key: str, value: dict | None) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def drop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


LEGACY_RECOVERY_DAYS = 7


class AwsSecretStore:
    """AWS Secrets Manager. The EC2 instance role needs secretsmanager:GetSecretValue,
    CreateSecret, PutSecretValue, DeleteSecret, RestoreSecret, DescribeSecret and TagResource on
    arn:aws:secretsmanager:<region>:<account>:secret:<prefix>* (see docs/iam-policy.json),
    in the legacy region too while SECRETS_LEGACY_REGION is set."""

    backend = "aws"

    def __init__(self, prefix: str = "es-config-api/", region: str | None = None,
                 kms_key_id: str | None = None, cache_seconds: float = 300, client=None,
                 endpoint_url: str | None = None, legacy_region: str | None = None,
                 legacy_client=None):
        self.prefix = prefix
        self.kms_key_id = kms_key_id
        cfg = Config(retries={"max_attempts": 5, "mode": "standard"})
        self.sm = client or boto3.client(
            "secretsmanager", region_name=region, endpoint_url=endpoint_url, config=cfg)
        self.region = region or self.sm.meta.region_name
        self.legacy_region = legacy_region if legacy_region and legacy_region != self.region else None
        self.legacy = None
        if self.legacy_region:
            self.legacy = legacy_client or boto3.client(
                "secretsmanager", region_name=self.legacy_region, config=cfg)
        self.cache = _Cache(cache_seconds)
        self._found: dict[str, str] = {}            # full name -> region it was read from

    def full_name(self, name: str) -> str:
        return _check(f"{self.prefix}{name}")

    @staticmethod
    def _read(sm, full: str) -> dict | None:
        try:
            resp = sm.get_secret_value(SecretId=full)
            return json.loads(resp.get("SecretString") or "{}")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return None
            if code == "InvalidRequestException" and "delet" in str(e).lower():
                return None                          # scheduled for deletion: treat as gone
            raise _aws_error("read", full, e) from e

    def get(self, name: str) -> dict | None:
        full = self.full_name(name)
        cached, value = self.cache.get(full)
        if cached:
            return value
        value = self._read(self.sm, full)
        where = self.region if value is not None else None
        if value is None and self.legacy is not None:
            value = self._read(self.legacy, full)
            where = self.legacy_region if value is not None else None
        if where:
            self._found[full] = where
        else:
            self._found.pop(full, None)
        self.cache.set(full, value)
        return value

    def where(self, name: str) -> str | None:
        """Region the secret is read from (main region first), or None if it doesn't exist."""
        full = self.full_name(name)
        self.cache.drop(full)
        self.get(name)
        return self._found.get(full)

    def _write_main(self, full: str, body: str, description: str) -> None:
        try:
            self.sm.put_secret_value(SecretId=full, SecretString=body)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                kwargs = {"Name": full, "SecretString": body,
                          "Description": description or "ES Config API",
                          "Tags": [{"Key": "app", "Value": "es-config-api"}]}
                if self.kms_key_id:
                    kwargs["KmsKeyId"] = self.kms_key_id
                try:
                    self.sm.create_secret(**kwargs)
                except ClientError as e2:
                    raise _aws_error("create", full, e2) from e2
            elif code == "InvalidRequestException" and "delet" in str(e).lower():
                try:
                    self.sm.restore_secret(SecretId=full)
                    self.sm.put_secret_value(SecretId=full, SecretString=body)
                except ClientError as e2:
                    raise _aws_error("restore", full, e2) from e2
            else:
                raise _aws_error("write", full, e) from e

    def _retire_legacy(self, full: str) -> bool:
        """Schedule the legacy-region copy for deletion (recoverable for 7 days).
        True if a live copy was there."""
        if self.legacy is None:
            return False
        try:
            self.legacy.delete_secret(SecretId=full, RecoveryWindowInDays=LEGACY_RECOVERY_DAYS)
            log.warning("Moved secret %s to %s; the %s copy is scheduled for deletion in %d days",
                        full, self.region, self.legacy_region, LEGACY_RECOVERY_DAYS)
            return True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return False
            if code == "InvalidRequestException" and "delet" in str(e).lower():
                return False                         # already scheduled
            raise _aws_error("delete", f"{full} ({self.legacy_region})", e) from e

    def put(self, name: str, value: dict, description: str = "") -> None:
        full = self.full_name(name)
        body = json.dumps(value, separators=(",", ":"))
        self._write_main(full, body, description)
        self.cache.set(full, value)
        self._found[full] = self.region
        if self.legacy is not None:
            try:                      # the new value is stored; retiring the old copy is best effort
                if self._found_in_legacy(full):
                    self._retire_legacy(full)
            except ApiError as e:
                log.warning("Stored %s in %s, but could not retire the %s copy: %s (migrate-secrets "
                            "will retry)", full, self.region, self.legacy_region, e.message)

    def _found_in_legacy(self, full: str) -> bool:
        try:
            d = self.legacy.describe_secret(SecretId=full)
            return not d.get("DeletedDate")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return False
            raise _aws_error("read", f"{full} ({self.legacy_region})", e) from e

    def delete(self, name: str) -> None:
        full = self.full_name(name)
        for sm, region in ((self.sm, self.region), (self.legacy, self.legacy_region)):
            if sm is None:
                continue
            try:
                sm.delete_secret(SecretId=full, ForceDeleteWithoutRecovery=True)
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise _aws_error("delete", full if sm is self.sm else f"{full} ({region})", e) from e
        self.cache.set(full, None)
        self._found.pop(full, None)

    def migrate(self, name: str, dry_run: bool = True) -> str:
        """Move one secret from the legacy region. Returns what happened (or would):
        'moved', 'retired' (already in the main region; legacy copy scheduled for deletion),
        'in-main', 'missing'. Values are copied unchanged."""
        full = self.full_name(name)
        if self.legacy is None:
            return "in-main" if self._read(self.sm, full) is not None else "missing"
        main_v = self._read(self.sm, full)
        legacy_live = self._found_in_legacy(full)
        if main_v is not None:
            if legacy_live and not dry_run:
                self._retire_legacy(full)
            return "retired" if legacy_live else "in-main"
        if not legacy_live:
            return "missing"
        if dry_run:
            return "moved"
        value = self._read(self.legacy, full)
        if value is None:
            return "missing"
        desc = self.legacy.describe_secret(SecretId=full).get("Description") or "ES Config API"
        self._write_main(full, json.dumps(value, separators=(",", ":")), desc)
        self.cache.set(full, value)
        self._found[full] = self.region
        self._retire_legacy(full)
        return "moved"


def _aws_error(what: str, name: str, e: ClientError) -> ApiError:
    code = e.response["Error"]["Code"]
    msg = e.response["Error"].get("Message", str(e))
    if code in ("AccessDeniedException", "AccessDenied", "UnrecognizedClientException"):
        return ApiError(500, "SECRETS_ACCESS_DENIED",
                        f"The API may not {what} secret '{name}' in AWS Secrets Manager: add the "
                        "secretsmanager permissions from docs/iam-policy.json to the EC2 role",
                        {"awsError": code})
    return ApiError(502, "SECRETS_UNAVAILABLE",
                    f"AWS Secrets Manager could not {what} '{name}': {msg}", {"awsError": code})


class LocalSecretStore:
    """Files on disk, one JSON file per secret, mode 600. For local development and tests."""

    backend = "local"

    def __init__(self, directory: str, prefix: str = "es-config-api/"):
        self.dir = Path(directory)
        self.prefix = prefix
        self._lock = threading.Lock()

    def full_name(self, name: str) -> str:
        return _check(f"{self.prefix}{name}")

    def _path(self, full: str) -> Path:
        return self.dir / (quote(full, safe="") + ".json")

    def get(self, name: str) -> dict | None:
        p = self._path(self.full_name(name))
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def put(self, name: str, value: dict, description: str = "") -> None:
        p = self._path(self.full_name(name))
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            tmp = p.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh)
            os.replace(tmp, p)

    def delete(self, name: str) -> None:
        try:
            self._path(self.full_name(name)).unlink()
        except FileNotFoundError:
            pass

    def where(self, name: str) -> str | None:
        return "local" if self._path(self.full_name(name)).exists() else None

    def migrate(self, name: str, dry_run: bool = True) -> str:
        return "in-main" if self.where(name) else "missing"


def build_secret_store(settings) -> SecretStore:
    if settings.secrets_backend == "aws":
        return AwsSecretStore(settings.secrets_prefix, settings.secrets_region or settings.aws_region,
                              settings.secrets_kms_key_id,
                              endpoint_url=settings.secrets_endpoint_url,
                              legacy_region=settings.secrets_legacy_region)
    return LocalSecretStore(settings.local_secrets_dir, settings.secrets_prefix)


# --------------------------------------------------------------- app-level secrets
APP_SECRET = "app"


def user_secret(username: str) -> str:
    return f"users/{username}"


def cluster_secret(cluster_id: str) -> str:
    return f"clusters/{cluster_id}"


def resolve_app_secrets(store: SecretStore, env_session_secret: str,
                        env_bootstrap_password: str | None, need_bootstrap_password: bool
                        ) -> tuple[str, str | None]:
    """(session secret, bootstrap admin password) from the app secret.

    First run: a SESSION_SECRET / BOOTSTRAP_ADMIN_PASSWORD still in .env is copied into the
    secret (so existing sessions stay valid); otherwise a session secret is generated. After
    that the .env values can be deleted."""
    doc = dict(store.get(APP_SECRET) or {})
    changed = False
    if not doc.get("sessionSecret"):
        doc["sessionSecret"] = env_session_secret if len(env_session_secret or "") >= 32 \
            else pysecrets.token_hex(32)
        changed = True
        log.warning("Stored the session secret in %s%s", store.prefix, APP_SECRET)
    if need_bootstrap_password and not doc.get("bootstrapAdminPassword") and env_bootstrap_password:
        doc["bootstrapAdminPassword"] = env_bootstrap_password
        changed = True
        log.warning("Copied BOOTSTRAP_ADMIN_PASSWORD from .env into %s%s; remove it from .env",
                    store.prefix, APP_SECRET)
    if changed:
        store.put(APP_SECRET, doc, "ES Config API: session signing key and first admin password")
    elif env_session_secret and env_session_secret != doc["sessionSecret"]:
        log.warning("SESSION_SECRET in .env is ignored: the value in %s%s is used. Remove it from .env",
                    store.prefix, APP_SECRET)
    return doc["sessionSecret"], doc.get("bootstrapAdminPassword")
