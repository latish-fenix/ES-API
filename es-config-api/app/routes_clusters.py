"""Admin: register, change, test and remove clusters: /api/v1/admin/clusters.

Clusters come from two places:
* ``clusters.yaml`` (CLUSTERS_FILE): edited on the server; read-only here.
* the managed clusters file (MANAGED_CLUSTERS_FILE, default /app/data/clusters.managed.yaml):
  clusters added through this API or the web console. It lives on the server's disk and
  holds connection details only; each cluster's password / API key is stored in AWS Secrets
  Manager (``<prefix>clusters/<id>``). Passwords and API keys are write-only: no endpoint
  ever returns them.
"""
from __future__ import annotations

import ssl
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .clusters import ClusterConfig, build_client, es_call, parse_cluster
from .errors import ApiError, bad_request, unprocessable
from .identity import User, require_admin
from .util import iso

router = APIRouter(prefix="/api/v1/admin/clusters", tags=["admin: clusters"])

PING_TIMEOUT = 8


class AuthBody(BaseModel):
    type: Literal["basic", "api_key", "none"] = Field(
        "basic", description="basic = username and password; api_key; none = no authentication "
                             "(security off). basic without a password is saved as none")
    username: str | None = Field(None, max_length=256)
    password: str | None = Field(None, max_length=1024,
                                 description="Write-only and optional. On update, leave out to keep "
                                             "the stored one; with none stored, the cluster is saved "
                                             "without authentication")
    apiKey: str | None = Field(None, max_length=2048,
                               description="Write-only (base64 'id:key' form). On update, leave out to keep")


class ClusterFields(BaseModel):
    name: str = Field("", max_length=100, description="Display name (defaults to the id)")
    description: str = Field("", max_length=500)
    hosts: list[str] = Field(..., min_length=1, max_length=20,
                             description="Node URLs, e.g. https://10.0.1.10:9200")
    auth: AuthBody = Field(default_factory=AuthBody)
    verifyCerts: bool = True
    caCertPem: str | None = Field(None, max_length=65536,
                                  description="PEM CA certificate for HTTPS. Update: leave out to "
                                              "keep, empty string to remove")
    requestTimeout: int = Field(30, ge=5, le=300)
    tags: list[str] = Field(default_factory=list, max_length=20)

    model_config = {"json_schema_extra": {"examples": [{
        "name": "ELK M2 Staging", "description": "Staging logs cluster",
        "hosts": ["https://10.0.2.10:9200"],
        "auth": {"type": "basic", "username": "es_console_api", "password": "…"},
        "verifyCerts": True, "caCertPem": "-----BEGIN CERTIFICATE-----\n…",
        "requestTimeout": 30, "tags": ["staging"]}]}}


class NewCluster(ClusterFields):
    id: str = Field(..., description="Lowercase letters, digits, - and _ (used in URLs; can't change later)")


class TestBody(ClusterFields):
    id: str | None = Field(None, description="An existing cluster whose stored secrets fill in "
                                             "a missing password / API key")


# ------------------------------------------------------------------ helpers
def _registry(request: Request):
    return request.app.state.registry


def _audit(request: Request, admin: User, action: str, **fields) -> None:
    fwd = request.headers.get("x-forwarded-for")
    request.app.state.audit.write({
        "action": action, "actor": admin.username, "outcome": "SUCCESS",
        "requestId": request.state.request_id,
        "sourceIp": fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None),
        **fields,
    })


def _check_hosts(hosts: list[str]) -> list[str]:
    out = []
    for h in hosts:
        h = h.strip().rstrip("/")
        u = urlparse(h)
        if u.scheme not in ("http", "https") or not u.hostname or " " in h:
            raise bad_request("INVALID_CLUSTER", f"'{h}' is not a node URL like https://10.0.1.10:9200")
        out.append(h)
    return out


def _check_pem(pem: str | None) -> str | None:
    if not pem or not pem.strip():
        return None
    pem = pem.strip() + "\n"
    if "BEGIN CERTIFICATE" not in pem:
        raise bad_request("INVALID_CLUSTER", "caCertPem must be a PEM certificate "
                          "(-----BEGIN CERTIFICATE----- …)")
    try:
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError) as e:
        raise bad_request("INVALID_CLUSTER", f"caCertPem could not be read: {e}") from e
    return pem


def _item(cid: str, body: ClusterFields, stored: dict | None, admin: User) -> dict:
    """Validated clusters.yaml-style entry; secrets left out of the body are kept."""
    old_auth = (stored or {}).get("auth") or {}
    a = body.auth
    auth: dict = {"type": a.type}
    if a.type == "basic":
        keep = old_auth.get("type") == "basic" and old_auth.get("username") == (a.username or "").strip()
        password = a.password if a.password else (old_auth.get("password") if keep else None)
        if not password:
            # The password is optional: a cluster without security needs none, so it is saved
            # (and connected to) without authentication.
            auth = {"type": "none"}
        else:
            if not (a.username or "").strip():
                raise bad_request("INVALID_CLUSTER", "A password needs a username")
            auth["username"] = a.username.strip()
            auth["password"] = password
    elif a.type == "api_key":
        auth["api_key"] = a.apiKey if a.apiKey else (
            old_auth.get("api_key") if old_auth.get("type") == "api_key" else None)
        if not auth["api_key"]:
            raise bad_request("INVALID_CLUSTER", "API key auth needs apiKey")
    if body.caCertPem is None:
        pem = (stored or {}).get("ca_cert_pem")
    else:
        pem = _check_pem(body.caCertPem)
    item = {
        "id": cid, "name": body.name.strip() or cid, "description": body.description.strip(),
        "hosts": _check_hosts(body.hosts), "auth": auth, "verify_certs": body.verifyCerts,
        "request_timeout": body.requestTimeout, "tags": [t.strip() for t in body.tags if t.strip()],
    }
    if pem:
        item["ca_cert_pem"] = pem
    now = iso()
    item.update({"createdAt": (stored or {}).get("createdAt", now),
                 "createdBy": (stored or {}).get("createdBy", admin.username),
                 "updatedAt": now, "updatedBy": admin.username})
    try:
        parse_cluster(item, "managed")
    except ValueError as e:
        raise bad_request("INVALID_CLUSTER", str(e)) from e
    return item


def test_connection(cfg: ClusterConfig, password: str | None = None, api_key: str | None = None) -> dict:
    """Connect with these settings (a throwaway client) and report what we found."""
    if cfg.auth_type == "basic" and not (password or cfg.password) or \
            cfg.auth_type == "api_key" and not (api_key or cfg.api_key):
        return {"reachable": False, "errorCode": "CLUSTER_CREDENTIALS_MISSING",
                "error": "No password or API key saved for this cluster"}
    es = build_client(cfg, password, api_key).options(request_timeout=min(cfg.request_timeout, PING_TIMEOUT), max_retries=0)
    out: dict = {"reachable": False}
    try:
        info = es_call(es, "GET", "/")
        version = (info.get("version") or {}).get("number")
        out.update(reachable=True, version=version, clusterName=info.get("cluster_name"),
                   clusterUuid=info.get("cluster_uuid"))
        try:
            h = es_call(es, "GET", "/_cluster/health")
            out.update(health=h.get("status"), numberOfNodes=h.get("number_of_nodes"))
        except ApiError as e:
            out["warnings"] = [f"Connected, but _cluster/health failed: {e.message}"]
        if version and not version.startswith("8."):
            out.setdefault("warnings", []).append(
                f"Elasticsearch {version}: this API is built and tested for 8.x")
    except ApiError as e:
        out.update(error=e.message, errorCode=e.code)
    finally:
        try:
            es.close()
        except Exception:
            pass
    return out


def _view(reg, cfg: ClusterConfig, ping: bool) -> dict:
    item = reg.view(cfg)
    if ping:
        try:
            pw, key = reg.credentials(cfg)
        except ApiError as e:
            item.update(reachable=False, error=e.message, errorCode=e.code)
            return item
        item.update(test_connection(cfg, pw, key))
    return item


# ------------------------------------------------------------------ routes
@router.get("", summary="All clusters (clusters.yaml + added here), optionally pinged")
def list_clusters(request: Request, check: bool = Query(True, description="Ping each cluster"),
                  admin: User = Depends(require_admin)):
    reg = _registry(request)
    clusters = sorted(reg.all(), key=lambda c: c.id)
    if check and clusters:
        with ThreadPoolExecutor(max_workers=min(8, len(clusters))) as pool:
            items = list(pool.map(lambda c: _view(reg, c, True), clusters))
    else:
        items = [_view(reg, c, False) for c in clusters]
    return {"items": items, "managedFile": reg.managed.path if reg.managed else None}


@router.get("/{cluster_id}", summary="One cluster (secrets never returned)")
def get_cluster(cluster_id: str, request: Request, check: bool = Query(False),
                admin: User = Depends(require_admin)):
    reg = _registry(request)
    return _view(reg, reg.get(cluster_id), check)


@router.post("/test", summary="Test connection settings without saving anything")
def test_cluster(body: TestBody, request: Request, admin: User = Depends(require_admin)):
    reg = _registry(request)
    stored = None
    if body.id and reg.managed and any(it.get("id") == body.id for it in reg.managed.read()):
        stored = reg.managed_item_with_secret(body.id)
    item = _item(body.id or "connection-test", body, stored, admin)
    return test_connection(parse_cluster(item, "managed"))


@router.post("", status_code=201, summary="Add a cluster (details on the server, password in Secrets Manager)")
def create_cluster(body: NewCluster, request: Request,
                   skipTest: bool = Query(False, description="Save even if the connection test fails"),
                   admin: User = Depends(require_admin)):
    reg = _registry(request)
    cid = body.id.strip()
    if cid in reg.ids():
        raise ApiError(409, "CLUSTER_EXISTS", f"A cluster with id '{cid}' already exists")
    item = _item(cid, body, None, admin)
    test = test_connection(parse_cluster(item, "managed"))
    if not test["reachable"] and not skipTest:
        raise unprocessable("CLUSTER_TEST_FAILED",
                            f"Could not connect: {test.get('error')}. Fix the settings, or save "
                            "anyway with skipTest=true", test)
    cfg = reg.save_managed(item, create=True)
    _audit(request, admin, "ADMIN_CLUSTER_CREATE", clusterId=cid, after=reg.view(cfg),
           connectionTest={k: test.get(k) for k in ("reachable", "version", "error")})
    return {"cluster": reg.view(cfg), "test": test}


@router.put("/{cluster_id}", summary="Change a cluster added here (clusters.yaml ones are read-only)")
def update_cluster(cluster_id: str, body: ClusterFields, request: Request,
                   skipTest: bool = Query(False), admin: User = Depends(require_admin)):
    reg = _registry(request)
    stored = reg.managed_item_with_secret(cluster_id)
    before = reg.view(reg.get(cluster_id))
    item = _item(cluster_id, body, stored, admin)
    test = test_connection(parse_cluster(item, "managed"))
    if not test["reachable"] and not skipTest:
        raise unprocessable("CLUSTER_TEST_FAILED",
                            f"Could not connect: {test.get('error')}. Nothing was changed. Fix the "
                            "settings, or save anyway with skipTest=true", test)
    cfg = reg.save_managed(item, create=False)
    pick = lambda a: {k: a.get(k) for k in ("type", "username", "password", "api_key")}  # noqa: E731
    old_auth, new_auth = pick(stored.get("auth") or {}), pick(item["auth"])
    _audit(request, admin, "ADMIN_CLUSTER_UPDATE", clusterId=cluster_id, before=before,
           after=reg.view(cfg), credentialsChanged=old_auth != new_auth,
           caCertChanged=stored.get("ca_cert_pem") != item.get("ca_cert_pem"))
    return {"cluster": reg.view(cfg), "test": test}


@router.delete("/{cluster_id}", summary="Remove a cluster added here")
def delete_cluster(cluster_id: str, request: Request,
                   confirm: str | None = Query(None, description="Repeat the cluster id"),
                   admin: User = Depends(require_admin)):
    reg = _registry(request)
    reg.managed_item(cluster_id)  # 404 / 409 read-only before asking for confirmation
    if confirm != cluster_id:
        raise bad_request("CONFIRMATION_MISMATCH", "Set 'confirm' to the cluster id to remove it",
                          {"expected": cluster_id, "got": confirm})
    before = reg.view(reg.get(cluster_id))
    reg.delete_managed(cluster_id)
    users = request.app.state.users.drop_cluster(cluster_id, admin.username)
    request.app.state.allowlist.delete(cluster_id)
    _audit(request, admin, "ADMIN_CLUSTER_DELETE", clusterId=cluster_id, before=before,
           removedFromUsers=users)
    return {"deleted": cluster_id, "removedFromUsers": users,
            "note": "Snapshots and audit history for this cluster stay in S3."}
