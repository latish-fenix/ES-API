"""Clusters added through the admin API: saved to a file on the server, never to S3."""
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
    assert saved["auth"]["password"] == API_PASSWORD and saved["createdBy"] == "root"
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
    assert saved["auth"]["password"] == API_PASSWORD and saved["createdBy"] == "root"
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
                      ({"auth": {"type": "basic", "username": "u"}}, "INVALID_CLUSTER"),
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


def test_management_can_be_switched_off(env):
    c = TestClient(env["app"])  # the default test app has no managed file
    r = c.post("/api/v1/admin/clusters", json=NEW, headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CLUSTER_MANAGEMENT_DISABLED"


def test_missing_clusters_yaml_is_ok(tmp_path):
    assert load_clusters(str(tmp_path / "nope.yaml")) == {}


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()
