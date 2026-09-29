"""Cluster registry loaded from clusters.yaml, plus one cached ES client per cluster.

Credentials are never written in the YAML itself; reference environment
variables instead, e.g. ``password: ${ES_PROD_US_PASSWORD}``.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import yaml
from elasticsearch import Elasticsearch
from elasticsearch import ApiError as EsApiError
from elasticsearch import TransportError as EsTransportError

from .errors import ApiError, not_found

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
    ca_certs: str | None = None
    request_timeout: int = 30
    tags: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "tags": self.tags}


def load_clusters(path: str) -> dict[str, ClusterConfig]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    missing: list[str] = []
    raw = _substitute(raw, missing)
    if missing:
        raise ValueError(f"clusters file references unset env vars: {sorted(set(missing))}")

    clusters: dict[str, ClusterConfig] = {}
    for item in raw.get("clusters", []):
        cid = str(item.get("id", ""))
        if not _ID_RE.match(cid):
            raise ValueError(f"invalid cluster id {cid!r} (lowercase letters, digits, - and _)")
        if cid in clusters:
            raise ValueError(f"duplicate cluster id {cid!r}")
        hosts = item.get("hosts") or ([item["url"]] if item.get("url") else [])
        if not hosts:
            raise ValueError(f"cluster {cid!r} needs 'url' or 'hosts'")
        auth = item.get("auth") or {}
        cfg = ClusterConfig(
            id=cid,
            name=item.get("name", cid),
            description=item.get("description", ""),
            hosts=list(hosts),
            auth_type=auth.get("type", "none" if not auth else "basic"),
            username=auth.get("username"),
            password=auth.get("password"),
            api_key=auth.get("api_key"),
            verify_certs=bool(item.get("verify_certs", True)),
            ca_certs=item.get("ca_certs"),
            request_timeout=int(item.get("request_timeout", 30)),
            tags=list(item.get("tags", [])),
        )
        if cfg.auth_type == "basic" and not (cfg.username and cfg.password):
            raise ValueError(f"cluster {cid!r}: basic auth needs username and password")
        if cfg.auth_type == "api_key" and not cfg.api_key:
            raise ValueError(f"cluster {cid!r}: api_key auth needs api_key")
        clusters[cid] = cfg
    return clusters


class ClusterRegistry:
    def __init__(self, clusters: dict[str, ClusterConfig]):
        self._clusters = clusters
        self._clients: dict[str, Elasticsearch] = {}
        self._lock = threading.Lock()

    def all(self) -> list[ClusterConfig]:
        return list(self._clusters.values())

    def ids(self) -> set[str]:
        return set(self._clusters)

    def get(self, cluster_id: str) -> ClusterConfig:
        cfg = self._clusters.get(cluster_id)
        if not cfg:
            raise not_found("CLUSTER_NOT_FOUND", f"Unknown cluster '{cluster_id}'")
        return cfg

    def client(self, cluster_id: str) -> Elasticsearch:
        cfg = self.get(cluster_id)
        with self._lock:
            if cluster_id not in self._clients:
                kwargs: dict[str, Any] = dict(
                    hosts=cfg.hosts, verify_certs=cfg.verify_certs,
                    request_timeout=cfg.request_timeout, retry_on_timeout=True, max_retries=2,
                )
                if cfg.ca_certs:
                    kwargs["ca_certs"] = cfg.ca_certs
                if cfg.auth_type == "basic":
                    kwargs["basic_auth"] = (cfg.username, cfg.password)
                elif cfg.auth_type == "api_key":
                    kwargs["api_key"] = cfg.api_key
                self._clients[cluster_id] = Elasticsearch(**kwargs)
            return self._clients[cluster_id]


def es_call(es: Elasticsearch, method: str, path: str, body: Any = None,
            params: dict | None = None) -> Any:
    """Raw ES request. Raises ApiError with a clean code on failure."""
    try:
        resp = es.perform_request(method, path, headers=ES_HEADERS, body=body, params=params)
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
