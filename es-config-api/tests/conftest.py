"""Integration tests: a real Elasticsearch 8.17 cluster + S3 mocked with moto.

    ES_TEST_URL=http://127.0.0.1:9200 ES_TEST_USER=elastic ES_TEST_PASSWORD=... pytest -q
"""
from __future__ import annotations

import os
import uuid

import boto3
import pytest
import requests
from fastapi.testclient import TestClient
from moto import mock_aws

from app.clusters import ClusterRegistry, load_clusters
from app.main import create_app
from app.settings import Settings
from app.storage import S3Store

ES_URL = os.environ.get("ES_TEST_URL", "http://127.0.0.1:9200")
ES_USER = os.environ.get("ES_TEST_USER", "elastic")
ES_PASSWORD = os.environ.get("ES_TEST_PASSWORD", "changeme123")
# Credentials the API itself uses (default: same as above). Set these to the
# config_api service account to prove docs/es-lockdown.md grants enough.
API_USER = os.environ.get("ES_TEST_API_USER", ES_USER)
API_PASSWORD = os.environ.get("ES_TEST_API_PASSWORD", ES_PASSWORD)
BUCKET = "es-config-api-test"


def _es_up() -> bool:
    try:
        return requests.get(ES_URL, auth=(ES_USER, ES_PASSWORD), timeout=3).ok
    except Exception:
        return False


if not _es_up():
    pytest.skip(f"Elasticsearch not reachable at {ES_URL}", allow_module_level=True)


class Es:
    """Direct ES access for arranging and checking tests (bypasses the API)."""

    def __call__(self, method, path, json=None, params=None, ok=True):
        r = requests.request(method, ES_URL + path, json=json, params=params,
                             auth=(ES_USER, ES_PASSWORD), timeout=30)
        if ok:
            assert r.status_code < 300, r.text
        return r.json() if r.content else None


@pytest.fixture(scope="session")
def es():
    return Es()


@pytest.fixture()
def prefix(es):
    p = f"cfgtest-{uuid.uuid4().hex[:8]}"
    yield p
    for row in es("GET", "/_cat/indices", params={"format": "json", "h": "index",
                                                  "expand_wildcards": "all"}, ok=False) or []:
        if row["index"].startswith(("cfgtest-", ".cfgtest-")):
            es("DELETE", f"/{row['index']}", ok=False)
    for ds in (es("GET", "/_data_stream", ok=False) or {}).get("data_streams", []):
        if ds["name"].startswith(p):
            es("DELETE", f"/_data_stream/{ds['name']}", ok=False)
    for t in (es("GET", "/_index_template", ok=False) or {}).get("index_templates", []):
        if t["name"].startswith(p):
            es("DELETE", f"/_index_template/{t['name']}", ok=False)
    for t in (es("GET", "/_component_template", ok=False) or {}).get("component_templates", []):
        if t["name"].startswith(p):
            es("DELETE", f"/_component_template/{t['name']}", ok=False)
    for name in (es("GET", "/_ingest/pipeline", ok=False) or {}):
        if name.startswith(p):
            es("DELETE", f"/_ingest/pipeline/{name}", ok=False)
    for name in (es("GET", "/_ilm/policy", ok=False) or {}):
        if name.startswith(p):
            es("DELETE", f"/_ilm/policy/{name}", ok=False)
    es("PUT", "/_cluster/settings", json={"persistent": {
        "indices.recovery.max_bytes_per_sec": None,
        "cluster.routing.allocation.disk.watermark.low": None,
        "cluster.routing.allocation.disk.watermark.high": None,
        "cluster.info.update.interval": None}}, ok=False)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ES_PASSWORD", API_PASSWORD)
    clusters_file = tmp_path / "clusters.yaml"
    clusters_file.write_text(f"""
clusters:
  - id: test
    name: Test cluster
    url: {ES_URL}
    auth:
      type: basic
      username: {API_USER}
      password: ${{TEST_ES_PASSWORD}}
  - id: other
    name: Other cluster
    url: {ES_URL}
    auth: {{type: basic, username: {API_USER}, password: "${{TEST_ES_PASSWORD}}"}}
""")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        settings = Settings(storage_backend="s3", s3_bucket=BUCKET, s3_prefix="t/", auth_mode="header",
                            clusters_file=str(clusters_file), bootstrap_admins=["root"],
                            lock_ttl_seconds=60)
        store = S3Store(BUCKET, "t/", client=s3)
        app = create_app(settings, store=store,
                         registry=ClusterRegistry(load_clusters(str(clusters_file))))
        yield {"app": app, "store": store, "s3": s3}


@pytest.fixture()
def client(env):
    return TestClient(env["app"])


def as_user(name: str) -> dict:
    return {"X-User": name}


ROOT = as_user("root")


@pytest.fixture()
def open_allowlist(client):
    """Allow everything in tests unless a test sets its own allowlist."""
    rules = {t: {"allow": ["*"]} for t in (
        "cluster-settings", "index-settings", "index-mappings", "index-templates",
        "component-templates", "ilm-policies", "ingest-pipelines")}
    r = client.put("/api/v1/admin/allowlist", json=rules, headers=ROOT)
    assert r.status_code == 200, r.text
    return rules
