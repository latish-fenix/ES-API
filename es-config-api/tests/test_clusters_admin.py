"""Clusters added through the admin API: details in a file on the server, the password in
Secrets Manager, nothing in S3."""
from __future__ import annotations

import os
import stat

import pytest
import yaml
from fastapi.testclient import TestClient

from app.clusters import ClusterRegistry, load_clusters
from app.main import create_app
from app.settings import Settings
from app.storage import S3Store

from conftest import API_PASSWORD, API_USER, BUCKET, ES_URL, ROOT, as_user

NEW = {
    "id": "added", "name": "Added cluster", "description": "added in a test",
    "hosts": [ES_URL + "/"], "auth": {"type": "basic", "username": API_USER, "password": API_PASSWORD},
    "verifyCerts": True, "requestTimeout": 20, "tags": ["test"],
}


@pytest.fixture()
def mapp(env, tmp_path):
    """An app whose registry also has a managed clusters file (like the Docker image)."""
    managed = tmp_path / "data" / "clusters.managed.yaml"
    s = env["app"].state.settings
    settings = Settings(storage_backend="s3", s3_bucket=BUCKET, s3_prefix="t/", auth_mode="header",
                        clusters_file=s.clusters_file, managed_clusters_file=str(managed),
                        bootstrap_admins=["root"], lock_ttl_seconds=60)
    app = create_app(settings, store=S3Store(BUCKET, "t/", client=env["s3"]))
    return {"app": app, "path": managed, "settings": settings, "s3": env["s3"]}


def test_add_use_update_remove(mapp, es, prefix):
    c = TestClient(mapp["app"])
    # 1) add: tested, saved to the file with 0600, secrets never returned
    r = c.post("/api/v1/admin/clusters", json=NEW, headers=ROOT)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["test"]["reachable"] and body["test"]["version"].startswith("8.")
    assert body["cluster"]["source"] == "managed" and body["cluster"]["editable"]
    assert body["cluster"]["hosts"] == [ES_URL]  # trailing slash trimmed
    assert "password" not in r.text and API_PASSWORD not in r.text
    assert stat.S_IMODE(os.stat(mapp["path"]).st_mode) == 0o600
    saved = yaml.safe_load(mapp["path"].read_text())["clusters"][0]
    assert "password" not in saved["auth"] and saved["auth"]["secret"] == "clusters/added"
    assert API_PASSWORD not in mapp["path"].read_text() and saved["createdBy"] == "root"
    assert _secret(mapp, "clusters/added") == {"password": API_PASSWORD}
    assert body["cluster"]["credentials"] == {"store": "secrets-manager", "present": True,
                                              "secretName": "es-config-api/clusters/added",
                                              "region": "us-east-1"}
    # nothing about clusters (let alone the password) went to S3
    keys = [o["Key"] for o in mapp["s3"].list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    assert not any("cluster" in k and "state/" in k for k in keys)
    for k in keys:
        assert API_PASSWORD.encode() not in mapp["s3"].get_object(Bucket=BUCKET, Key=k)["Body"].read()

    # 2) listed next to the clusters.yaml ones, and usable like any other cluster
    items = {i["id"]: i for i in c.get("/api/v1/admin/clusters", headers=ROOT).json()["items"]}
    assert items["test"]["source"] == "file" and not items["test"]["editable"]
    assert items["added"]["reachable"] is True and items["added"]["username"] == API_USER
    assert c.get("/api/v1/clusters/added/health", headers=ROOT).json()["status"] in ("green", "yellow")
    assert c.put("/api/v1/admin/users/ann", json={"clusters": {"added": "view"}}, headers=ROOT).status_code == 200
    assert [x["id"] for x in c.get("/api/v1/clusters", headers=as_user("ann")).json()["items"]] == ["added"]

    # 3) another API worker (a second registry on the same file) sees it
    other = ClusterRegistry(load_clusters(mapp["settings"].clusters_file), str(mapp["path"]))
    assert "added" in other.ids()

    # 4) update without resending the password keeps it; the change reaches other workers
    upd = {k: v for k, v in NEW.items() if k != "id"}
    upd = {**upd, "name": "Renamed", "auth": {"type": "basic", "username": API_USER}}
    r = c.put("/api/v1/admin/clusters/added", json=upd, headers=ROOT)
    assert r.status_code == 200, r.text
    assert r.json()["cluster"]["name"] == "Renamed"
    saved = yaml.safe_load(mapp["path"].read_text())["clusters"][0]
    assert "password" not in saved["auth"] and saved["createdBy"] == "root"
    assert _secret(mapp, "clusters/added") == {"password": API_PASSWORD}
    os.utime(mapp["path"], ns=(os.stat(mapp["path"]).st_atime_ns, os.stat(mapp["path"]).st_mtime_ns + 10**9))
    other.refresh(force=True)
    assert other.get("added").name == "Renamed"

    # 5) remove: needs the id repeated; access and the allowlist override go with it
    assert c.put("/api/v1/admin/allowlist/added", json={"cluster-settings": {"allow": ["x"]}},
                 headers=ROOT).status_code == 200
    r = c.delete("/api/v1/admin/clusters/added", headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    r = c.delete("/api/v1/admin/clusters/added", params={"confirm": "added"}, headers=ROOT)
    assert r.status_code == 200, r.text
    assert r.json()["removedFromUsers"] == ["ann"]
    assert c.get("/api/v1/admin/users/ann", headers=ROOT).json()["clusters"] == {}
    assert c.get("/api/v1/clusters/added/health", headers=ROOT).status_code == 404
    assert yaml.safe_load(mapp["path"].read_text())["clusters"] == []
    assert _secret(mapp, "clusters/added") is None  # the secret goes with the cluster

    acts = [e["action"] for e in c.get("/api/v1/admin/audit", params={"date": _today()},
                                       headers=ROOT).json()["items"]]
    assert {"ADMIN_CLUSTER_CREATE", "ADMIN_CLUSTER_UPDATE", "ADMIN_CLUSTER_DELETE"} <= set(acts)
    audit_text = str(c.get("/api/v1/admin/audit", params={"date": _today()}, headers=ROOT).json())
    assert API_PASSWORD not in audit_text


def test_refusals(mapp):
    c = TestClient(mapp["app"])
    # clusters.yaml ones are read-only here, and their ids are taken
    assert c.post("/api/v1/admin/clusters", json={**NEW, "id": "test"}, headers=ROOT).json()["error"]["code"] == "CLUSTER_EXISTS"
    r = c.put("/api/v1/admin/clusters/test", json={k: v for k, v in NEW.items() if k != "id"}, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CLUSTER_READ_ONLY"
    r = c.delete("/api/v1/admin/clusters/test", params={"confirm": "test"}, headers=ROOT)
    assert r.json()["error"]["code"] == "CLUSTER_READ_ONLY"
    # validation
    for bad, code in [({"id": "Bad Id"}, "INVALID_CLUSTER"), ({"hosts": ["10.0.0.1:9200"]}, "INVALID_CLUSTER"),
                      ({"caCertPem": "not a cert"}, "INVALID_CLUSTER")]:
        r = c.post("/api/v1/admin/clusters", json={**NEW, **bad}, headers=ROOT)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (bad, r.text)
    # wrong password: the connection test fails and nothing is saved, unless skipTest
    wrong = {**NEW, "id": "wrongpw", "auth": {"type": "basic", "username": API_USER, "password": "nope-nope"}}
    r = c.post("/api/v1/admin/clusters", json=wrong, headers=ROOT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "CLUSTER_TEST_FAILED"
    assert r.json()["error"]["details"]["errorCode"] == "ES_AUTH_FAILED"
    assert not mapp["path"].exists()
    unreachable = {**NEW, "id": "down", "hosts": ["http://127.0.0.1:1"], "requestTimeout": 5}
    r = c.post("/api/v1/admin/clusters/test", json=unreachable, headers=ROOT)
    assert r.status_code == 200 and r.json()["reachable"] is False
    r = c.post("/api/v1/admin/clusters", params={"skipTest": "true"}, json=unreachable, headers=ROOT)
    assert r.status_code == 201 and r.json()["test"]["reachable"] is False
    # admins only
    assert c.post("/api/v1/admin/clusters/test", json=NEW, headers=as_user("nobody")).status_code == 403


def test_password_is_optional(mapp):
    """A cluster without security needs no password: basic without one is saved as 'none'."""
    c = TestClient(mapp["app"])
    for body in ({**NEW, "id": "nopw", "auth": {"type": "basic", "username": "config_api"}},
                 {**NEW, "id": "nopw2", "auth": {"type": "basic"}},
                 {**NEW, "id": "nopw3", "auth": {"type": "none"}}):
        # the test ES has security on, so the unauthenticated test fails (401) but is reported
        t = c.post("/api/v1/admin/clusters/test", json=body, headers=ROOT)
        assert t.status_code == 200 and t.json()["errorCode"] == "ES_AUTH_FAILED", t.text
        r = c.post("/api/v1/admin/clusters", params={"skipTest": "true"}, json=body, headers=ROOT)
        assert r.status_code == 201, r.text
        cl = r.json()["cluster"]
        assert cl["authType"] == "none" and cl["credentials"] == {"store": "none"}
        assert _secret(mapp, f"clusters/{body['id']}") is None
    saved = {i["id"]: i for i in yaml.safe_load(mapp["path"].read_text())["clusters"]}
    assert saved["nopw"]["auth"] == {"type": "none"}
    # editing a cluster with a saved password and leaving the password out keeps it
    r = c.post("/api/v1/admin/clusters", json=NEW, headers=ROOT)
    assert r.status_code == 201
    upd = {k: v for k, v in NEW.items() if k != "id"}
    r = c.put("/api/v1/admin/clusters/added", json={**upd, "auth": {"type": "basic", "username": API_USER}}, headers=ROOT)
    assert r.status_code == 200 and r.json()["cluster"]["authType"] == "basic"
    assert _secret(mapp, "clusters/added") == {"password": API_PASSWORD}
    # a password without a username is refused
    r = c.post("/api/v1/admin/clusters", json={**NEW, "id": "nouser", "auth": {"type": "basic", "password": "x"}}, headers=ROOT)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_CLUSTER"


def test_management_can_be_switched_off(env):
    c = TestClient(env["app"])  # the default test app has no managed file
    r = c.post("/api/v1/admin/clusters", json=NEW, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CLUSTER_MANAGEMENT_DISABLED"


def test_missing_clusters_yaml_is_ok(tmp_path):
    assert load_clusters(str(tmp_path / "nope.yaml")) == {}


def _secret(mapp, name):
    return mapp["app"].state.secrets.get(name)


def test_inline_passwords_move_to_secrets_manager(env, tmp_path):
    """A managed file from before Secrets Manager (password inline) is migrated at start."""
    managed = tmp_path / "clusters.managed.yaml"
    managed.write_text(yaml.safe_dump({"clusters": [{"id": "legacy", "url": ES_URL, "auth": {
        "type": "basic", "username": API_USER, "password": API_PASSWORD}}]}))
    s = env["app"].state.settings
    settings = Settings(storage_backend="s3", s3_bucket=BUCKET, s3_prefix="t/", auth_mode="header",
                        clusters_file=s.clusters_file, managed_clusters_file=str(managed),
                        bootstrap_admins=["root"])
    app = create_app(settings, store=S3Store(BUCKET, "t/", client=env["s3"]))
    assert API_PASSWORD not in managed.read_text()
    assert app.state.secrets.get("clusters/legacy") == {"password": API_PASSWORD}
    # the clusters.yaml password (from ${TEST_ES_PASSWORD}) was copied into its secret too
    assert app.state.secrets.get("clusters/test") == {"password": API_PASSWORD}
    c = TestClient(app)
    assert c.get("/api/v1/clusters/legacy/health", headers=ROOT).status_code == 200


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()
