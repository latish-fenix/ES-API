"""Cluster registry: clusters.yaml (read-only) + clusters added through the API, with one
cached ES client per cluster.

Passwords and API keys live in AWS Secrets Manager (``<prefix>clusters/<id>``, a JSON
object ``{"password": ...}`` or ``{"apiKey": ...}``), never in either YAML file. A
clusters.yaml entry without a password uses that secret (or the one named in
``auth.secret``). ``${ENV}`` references still work for local development; at startup such
a value is copied into Secrets Manager so it can be removed from .env.
"""
from __future__ import annotations

import logging
import os
import re
import ssl
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import yaml
from elasticsearch import Elasticsearch
from elasticsearch import ApiError as EsApiError
from elasticsearch import TransportError as EsTransportError

from .errors import ApiError, not_found

try:  # POSIX (the Docker image, EC2)
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows (local-test scripts)
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

log = logging.getLogger("es_config_api")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

ES_HEADERS = {
    "accept": "application/vnd.elasticsearch+json; compatible-with=8",
    "content-type": "application/vnd.elasticsearch+json; compatible-with=8",
}


def _substitute(value: Any, missing: list[str]) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.append(name)
            return ""
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, missing) for v in value]
    return value


@dataclass
class ClusterConfig:
    id: str
    name: str
    hosts: list[str]
    description: str = ""
    auth_type: str = "basic"          # basic | api_key | none
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    verify_certs: bool = True
    ca_certs: str | None = None       # path to a CA bundle (clusters.yaml)
    ca_cert_pem: str | None = None    # CA certificate text (clusters added through the API)
    request_timeout: int = 30
    tags: list[str] = field(default_factory=list)
    source: str = "file"              # file = clusters.yaml, managed = added through the API
    meta: dict = field(default_factory=dict)  # createdAt/By, updatedAt/By (managed only)
    secret: str | None = None         # secret holding the password / API key (relative name)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "tags": self.tags}

    def admin_view(self) -> dict:
        """Everything an admin may see: never the password or API key."""
        return {**self.public(), "hosts": self.hosts, "authType": self.auth_type,
                "username": self.username if self.auth_type == "basic" else None,
                "verifyCerts": self.verify_certs,
                "hasCaCert": bool(self.ca_certs or self.ca_cert_pem),
                "requestTimeout": self.request_timeout, "source": self.source,
                "editable": self.source == "managed", **self.meta}


def parse_cluster(item: dict, source: str) -> ClusterConfig:
    """One cluster entry (clusters.yaml format) -> ClusterConfig; ValueError if invalid."""
    cid = str(item.get("id", ""))
    if not _ID_RE.match(cid):
        raise ValueError(f"invalid cluster id {cid!r} (lowercase letters, digits, - and _; "
                         "up to 63 characters, starting with a letter or digit)")
    hosts = item.get("hosts") or ([item["url"]] if item.get("url") else [])
    if isinstance(hosts, str):
        hosts = [hosts]
    if not hosts:
        raise ValueError(f"cluster {cid!r} needs 'url' or 'hosts'")
    auth = item.get("auth") or {}
    inline = auth.get("password") or auth.get("api_key")
    cfg = ClusterConfig(
        id=cid,
        name=str(item.get("name") or cid),
        description=str(item.get("description") or ""),
        hosts=[str(h) for h in hosts],
        auth_type=auth.get("type", "none" if not auth else "basic"),
        username=auth.get("username"),
        password=auth.get("password"),
        api_key=auth.get("api_key"),
        verify_certs=bool(item.get("verify_certs", True)),
        ca_certs=item.get("ca_certs"),
        ca_cert_pem=item.get("ca_cert_pem"),
        request_timeout=int(item.get("request_timeout", 30)),
        tags=[str(t) for t in item.get("tags", [])],
        source=source,
        meta={k: item[k] for k in ("createdAt", "createdBy", "updatedAt", "updatedBy") if k in item},
        secret=auth.get("secret") or (None if inline else f"clusters/{cid}"),
    )
    if cfg.auth_type not in ("basic", "api_key", "none"):
        raise ValueError(f"cluster {cid!r}: auth type must be basic, api_key or none")
    if cfg.auth_type == "basic" and not cfg.username:
        raise ValueError(f"cluster {cid!r}: basic auth needs a username")
    if cfg.auth_type == "none":
        cfg.secret = None
    return cfg


def load_clusters(path: str, secrets=None) -> dict[str, ClusterConfig]:
    """clusters.yaml (with ${ENV} substitution). A missing file means no file clusters."""
    # Docker creates an empty directory when a bind-mounted file is missing: treat as none.
    if not os.path.isfile(path):
        log.warning("clusters file %s not found; only clusters added through the API are used", path)
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    missing: list[str] = []
    raw = _substitute(raw, missing)
    if missing:
        raise ValueError(f"clusters file references unset env vars: {sorted(set(missing))}")

    clusters: dict[str, ClusterConfig] = {}
    for item in raw.get("clusters", []) or []:
        cfg = parse_cluster(item, "file")
        if cfg.id in clusters:
            raise ValueError(f"duplicate cluster id {cfg.id!r}")
        clusters[cfg.id] = cfg
    return clusters


class ManagedClustersFile:
    """Clusters added through the API, kept in a YAML file on the server (not in S3).

    Only connection details: the password / API key of each cluster is in Secrets Manager
    (``auth.secret``). Written with mode 0600, atomically (temp file + rename), under an
    exclusive lock so two API workers never interleave writes. Same format as clusters.yaml.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock_path = path + ".lock"

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def mtime(self) -> float | None:
        try:
            return os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return None

    def read(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            return []
        return list(raw.get("clusters", []) or [])

    @contextmanager
    def locked(self):
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as e:
            raise ApiError(500, "CLUSTERS_FILE_NOT_WRITABLE",
                           f"Can't write {self.path}: {e.strerror}. On the server, make the "
                           "data folder writable by the container (see README)") from e
        try:
            _lock(fd)
            yield
        finally:
            _unlock(fd)
            os.close(fd)

    def write(self, items: list[dict]) -> None:
        folder = os.path.dirname(self.path) or "."
        header = ("# Clusters added through the ES Config API (admin > Clusters).\n"
                  "# Managed by the API. Passwords / API keys are in Secrets Manager (auth.secret).\n")
        body = yaml.safe_dump({"clusters": items}, sort_keys=False, allow_unicode=True)
        try:
            fd, tmp = tempfile.mkstemp(prefix=".clusters-", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(header + body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            raise ApiError(500, "CLUSTERS_FILE_NOT_WRITABLE",
                           f"Can't write {self.path}: {e.strerror}") from e


class ClusterRegistry:
    """File clusters (read-only) + managed clusters (re-read when their file changes)."""

    RECHECK_SECONDS = 1.0

    def __init__(self, clusters: dict[str, ClusterConfig], managed_file: str | None = None,
                 secrets=None):
        self._file = dict(clusters)
        self.managed = ManagedClustersFile(managed_file) if managed_file else None
        self._managed: dict[str, ClusterConfig] = {}
        self._managed_mtime: float | None = None
        self._checked = 0.0
        self._clients: dict[str, tuple[tuple, Elasticsearch]] = {}
        self._lock = threading.RLock()
        self.secrets = None
        self.refresh(force=True)
        if secrets is not None:
            self.attach_secrets(secrets)

    # -- secrets --------------------------------------------------------------
    def attach_secrets(self, secrets) -> None:
        """Use this secret store and move any password still in a file into it."""
        self.secrets = secrets
        self._migrate_secrets()

    def _migrate_secrets(self) -> None:
        # clusters.yaml: a password from ${ENV} is copied into the cluster's secret (once),
        # so the env var can be deleted. The YAML itself is never rewritten.
        for cfg in self._file.values():
            value = _secret_value(cfg.password, cfg.api_key)
            name = f"clusters/{cfg.id}"
            if value and not self.secrets.get(name):
                self.secrets.put(name, value, f"ES Config API: credentials for cluster {cfg.id}")
                log.warning("Copied the credentials of cluster %s (clusters.yaml) into %s; remove "
                            "the password line and its .env variable", cfg.id,
                            self.secrets.full_name(name))
        # managed file: inline credentials move to Secrets Manager and leave the file
        if not self.managed or not self.managed.exists():
            return
        with self.managed.locked():
            items, moved = self.managed.read(), []
            for it in items:
                auth = it.get("auth") or {}
                value = _secret_value(auth.get("password"), auth.get("api_key"))
                if value:
                    name = auth.get("secret") or f"clusters/{it['id']}"
                    self.secrets.put(name, value, f"ES Config API: credentials for cluster {it['id']}")
                    auth.pop("password", None)
                    auth.pop("api_key", None)
                    auth["secret"] = name
                    it["auth"] = auth
                    moved.append(it["id"])
            if moved:
                self.managed.write(items)
                log.warning("Moved the credentials of %s from %s into %s", ", ".join(moved), self.managed.path,
                            "AWS Secrets Manager" if self.secrets.backend == "aws" else "the local secret store")
        self.refresh(force=True)

    def credentials(self, cfg: ClusterConfig) -> tuple[str | None, str | None]:
        """(password, api key) for a cluster: inline (dev) or from its secret."""
        if cfg.password or cfg.api_key or not cfg.secret:
            return cfg.password, cfg.api_key
        if self.secrets is None:
            return None, None
        doc = self.secrets.get(cfg.secret) or {}
        return doc.get("password"), doc.get("apiKey")

    def stored_secret(self, cluster_id: str) -> dict:
        cfg = self.get(cluster_id)
        if not cfg.secret or self.secrets is None:
            return {}
        return dict(self.secrets.get(cfg.secret) or {})

    def view(self, cfg: ClusterConfig) -> dict:
        """admin_view + where the credentials live (never the credentials)."""
        out = cfg.admin_view()
        if cfg.auth_type == "none":
            out["credentials"] = {"store": "none"}
        elif cfg.password or cfg.api_key:
            out["credentials"] = {"store": "inline"}
        else:
            pw, key = self.credentials(cfg)
            out["credentials"] = {
                "store": "secrets-manager" if getattr(self.secrets, "backend", "") == "aws" else "local-secrets",
                "secretName": self.secrets.full_name(cfg.secret) if self.secrets else cfg.secret,
                "present": bool(pw or key)}
            region = getattr(self.secrets, "_found", {}).get(self.secrets.full_name(cfg.secret)) \
                if self.secrets is not None and (pw or key) else None
            if region:
                out["credentials"]["region"] = region
        return out

    # -- reading --------------------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        if not self.managed:
            return
        now = time.monotonic()
        if not force and now - self._checked < self.RECHECK_SECONDS:
            return
        with self._lock:
            self._checked = now
            mtime = self.managed.mtime()
            if not force and mtime == self._managed_mtime:
                return
            loaded: dict[str, ClusterConfig] = {}
            for item in self.managed.read():
                try:
                    cfg = parse_cluster(item, "managed")
                except ValueError as e:
                    log.error("ignoring invalid entry in %s: %s", self.managed.path, e)
                    continue
                if cfg.id in self._file:
                    log.error("ignoring %s in %s: the id is already used in clusters.yaml",
                              cfg.id, self.managed.path)
                    continue
                loaded[cfg.id] = cfg
            self._managed, self._managed_mtime = loaded, mtime

    def _all(self) -> dict[str, ClusterConfig]:
        self.refresh()
        return {**self._file, **self._managed}

    def all(self) -> list[ClusterConfig]:
        return list(self._all().values())

    def ids(self) -> set[str]:
        return set(self._all())

    def get(self, cluster_id: str) -> ClusterConfig:
        cfg = self._all().get(cluster_id)
        if not cfg:
            raise not_found("CLUSTER_NOT_FOUND", f"Unknown cluster '{cluster_id}'")
        return cfg

    def client(self, cluster_id: str) -> Elasticsearch:
        cfg = self.get(cluster_id)
        pw, key = self.credentials(cfg)
        if cfg.auth_type == "basic" and not pw or cfg.auth_type == "api_key" and not key:
            raise ApiError(502, "CLUSTER_CREDENTIALS_MISSING",
                           f"No password or API key for cluster '{cluster_id}': create the secret "
                           f"{self.secrets.full_name(cfg.secret) if self.secrets and cfg.secret else cfg.secret} "
                           "in Secrets Manager (see README)")
        fp = (repr(cfg), pw, key)
        with self._lock:
            hit = self._clients.get(cluster_id)
            if hit is None or hit[0] != fp:  # new cluster, changed config or rotated secret
                self._clients[cluster_id] = (fp, build_client(cfg, pw, key))
            return self._clients[cluster_id][1]

    # -- managed clusters -----------------------------------------------------
    def _require_managed(self) -> ManagedClustersFile:
        if not self.managed:
            raise ApiError(409, "CLUSTER_MANAGEMENT_DISABLED",
                           "Adding clusters through the API is off (MANAGED_CLUSTERS_FILE is empty)")
        return self.managed

    def save_managed(self, item: dict, *, create: bool) -> ClusterConfig:
        """Create or replace one managed cluster entry (already validated). Its password /
        API key goes to Secrets Manager; the file keeps only a reference to the secret."""
        mf = self._require_managed()
        if self.secrets is None:
            raise ApiError(500, "SECRETS_NOT_CONFIGURED", "No secret store is configured")
        item = json_copy(item)
        auth = item.get("auth") or {}
        value = _secret_value(auth.pop("password", None), auth.pop("api_key", None))
        name = f"clusters/{item['id']}"
        if auth.get("type", "basic") != "none":
            auth["secret"] = name
        item["auth"] = auth
        cfg = parse_cluster(item, "managed")
        if create and (cfg.id in self._file or any(it.get("id") == cfg.id for it in mf.read())):
            raise ApiError(409, "CLUSTER_EXISTS", f"A cluster with id '{cfg.id}' already exists")
        if value:
            self.secrets.put(name, value, f"ES Config API: credentials for cluster {cfg.id}")
        elif auth.get("type") == "none":
            self.secrets.delete(name)
        with mf.locked():
            items = mf.read()
            idx = next((i for i, it in enumerate(items) if it.get("id") == cfg.id), None)
            if create:
                if cfg.id in self._file or idx is not None:
                    raise ApiError(409, "CLUSTER_EXISTS", f"A cluster with id '{cfg.id}' already exists")
                items.append(item)
            else:
                if idx is None:
                    raise self._not_managed(cfg.id)
                items[idx] = item
            mf.write(items)
        self.refresh(force=True)
        return self.get(cfg.id)

    def managed_item_with_secret(self, cluster_id: str) -> dict:
        """The file entry with its saved password / API key filled in (server-side use only)."""
        item = json_copy(self.managed_item(cluster_id))
        auth = item.setdefault("auth", {})
        doc = self.secrets.get(auth["secret"]) if self.secrets and auth.get("secret") else None
        if doc:
            if doc.get("password"):
                auth["password"] = doc["password"]
            if doc.get("apiKey"):
                auth["api_key"] = doc["apiKey"]
        return item

    def managed_item(self, cluster_id: str) -> dict:
        mf = self._require_managed()
        item = next((it for it in mf.read() if it.get("id") == cluster_id), None)
        if item is None:
            raise self._not_managed(cluster_id)
        return item

    def delete_managed(self, cluster_id: str) -> dict:
        mf = self._require_managed()
        with mf.locked():
            items = mf.read()
            keep = [it for it in items if it.get("id") != cluster_id]
            if len(keep) == len(items):
                raise self._not_managed(cluster_id)
            mf.write(keep)
        if self.secrets is not None:
            self.secrets.delete(f"clusters/{cluster_id}")
        with self._lock:
            self._clients.pop(cluster_id, None)
        self.refresh(force=True)
        return {"deleted": cluster_id}

    def _not_managed(self, cluster_id: str) -> ApiError:
        if cluster_id in self._file:
            return ApiError(409, "CLUSTER_READ_ONLY",
                            f"Cluster '{cluster_id}' is defined in clusters.yaml on the server; "
                            "change it there")
        return not_found("CLUSTER_NOT_FOUND", f"Unknown cluster '{cluster_id}'")


def _secret_value(password: str | None, api_key: str | None) -> dict | None:
    if api_key:
        return {"apiKey": api_key}
    if password:
        return {"password": password}
    return None


def json_copy(obj: Any) -> Any:
    import json
    return json.loads(json.dumps(obj))


def build_client(cfg: ClusterConfig, password: str | None = None,
                 api_key: str | None = None) -> Elasticsearch:
    kwargs: dict[str, Any] = dict(
        hosts=cfg.hosts, verify_certs=cfg.verify_certs,
        request_timeout=cfg.request_timeout, retry_on_timeout=True, max_retries=2,
    )
    if cfg.ca_cert_pem and cfg.verify_certs:
        kwargs["ssl_context"] = ssl.create_default_context(cadata=cfg.ca_cert_pem)
        kwargs.pop("verify_certs")
    elif cfg.ca_certs:
        kwargs["ca_certs"] = cfg.ca_certs
    if not cfg.verify_certs:
        kwargs["ssl_show_warn"] = False
    if cfg.auth_type == "basic":
        kwargs["basic_auth"] = (cfg.username, password or cfg.password)
    elif cfg.auth_type == "api_key":
        kwargs["api_key"] = api_key or cfg.api_key
    return Elasticsearch(**kwargs)


NDJSON_HEADERS = {
    "accept": "application/vnd.elasticsearch+json; compatible-with=8",
    "content-type": "application/vnd.elasticsearch+x-ndjson; compatible-with=8",
}


def es_call(es: Elasticsearch, method: str, path: str, body: Any = None,
            params: dict | None = None, headers: dict | None = None) -> Any:
    """Raw ES request. Raises ApiError with a clean code on failure."""
    try:
        resp = es.perform_request(method, path, headers=headers or ES_HEADERS, body=body, params=params)
        return resp.body
    except EsApiError as e:
        status = getattr(e.meta, "status", 500)
        reason = _es_reason(e.body) or str(e)
        if status == 404:
            raise ApiError(404, "ES_NOT_FOUND", reason, {"esStatus": status}) from e
        if status in (401, 403):
            raise ApiError(502, "ES_AUTH_FAILED",
                           f"Elasticsearch refused the API's credentials: {reason}",
                           {"esStatus": status}) from e
        if status == 429 or status >= 500:
            raise ApiError(502, "ES_UNAVAILABLE", f"Elasticsearch error {status}: {reason}",
                           {"esStatus": status}) from e
        raise ApiError(400, "ES_REJECTED", reason, {"esStatus": status, "esError": e.body}) from e
    except EsTransportError as e:
        raise ApiError(502, "CLUSTER_UNREACHABLE", f"Could not reach cluster: {e}") from e


def _es_reason(body: Any) -> str | None:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            causes = err.get("root_cause") or []
            if causes and isinstance(causes[0], dict) and causes[0].get("reason"):
                return causes[0]["reason"]
            return err.get("reason")
        if isinstance(err, str):
            return err
    return None
