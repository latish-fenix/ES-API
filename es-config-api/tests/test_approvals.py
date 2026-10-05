"""Approvals: non-admin changes wait for an admin, run as the requester after approval, are
refused if the resource changed meanwhile; admins and requesters are emailed."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.mailer import RecordingMailer
from conftest import ROOT, as_user

API = "/api/v1/clusters/test"
DEV = "dev@fenixcommerce.com"
BOSS = "boss@fenixcommerce.com"
on = pytest.mark.parametrize("env", [True], indirect=True)


@pytest.fixture()
def mail(env, client, open_allowlist, prefix):
    svc = env["app"].state.approvals
    svc.mailer, svc.async_mail = RecordingMailer(), False
    svc.settings = type(svc.settings)(**{**svc.settings.__dict__, "public_url": "http://console.test"})
    client.put(f"/api/v1/admin/users/{BOSS}", json={"admin": True}, headers=ROOT)
    client.put(f"/api/v1/admin/users/{DEV}", json={"clusters": {"test": {"default": "view", "indices": [
        {"pattern": f"{prefix}-*", "level": "edit"}]}}}, headers=ROOT)
    client.put("/api/v1/admin/users/looker", json={"clusters": {"test": "view"}}, headers=ROOT)
    return svc.mailer


def _audit(client, **f):
    day = datetime.now(timezone.utc).date().isoformat()
    items = client.get("/api/v1/admin/audit", params={"date": day}, headers=ROOT).json()["items"]
    return [e for e in items if all(e.get(k) == v for k, v in f.items())]


@on
def test_settings_change_waits_then_applies(env, client, es, prefix, mail):
    idx = f"{prefix}-idx"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    url = f"{API}/indices/{idx}/settings"
    body = {"config": {"refresh_interval": "31s"}, "reason": "slower refresh for the import"}
    dev = as_user(DEV)
    # dry run is still immediate
    assert client.put(url, params={"dryRun": "true"}, json=body, headers=dev).json()["dryRun"] is True
    # no reason: refused before anything is saved
    assert client.put(url, json={"config": body["config"]}, headers=dev).json()["error"]["code"] == "REASON_REQUIRED"
    r = client.put(url, json=body, headers=dev)
    assert r.status_code == 202, r.text
    req = r.json()["approval"]
    assert r.json()["pendingApproval"] and req["status"] == "PENDING" and req["label"] == "Change index settings"
    assert req["summary"]["fields"] == ["index.refresh_interval"]
    rid = req["id"]
    s = es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]
    assert "index.refresh_interval" not in s                       # not applied yet
    # admins emailed (not the requester), names only, with the link
    assert [m["to"] for m in mail.messages] == [BOSS]
    m = mail.messages[0]
    assert "Approval needed" in m["subject"] and DEV in m["subject"]
    assert "index.refresh_interval" in m["text"] and "31s" not in m["text"]
    assert f"http://console.test/ui/requests/{rid}" in m["text"]
    # who sees what
    assert client.get("/api/v1/approvals/_count", headers=ROOT).json() == {"pending": 1, "mine": 0}
    assert client.get("/api/v1/approvals/_count", headers=dev).json() == {"pending": 0, "mine": 1}
    assert [i["id"] for i in client.get("/api/v1/approvals", headers=dev).json()["items"]] == [rid]
    assert client.get("/api/v1/approvals", params={"scope": "pending"}, headers=dev).status_code == 403
    assert client.get(f"/api/v1/approvals/{rid}", headers=as_user("looker")).status_code == 403
    full = client.get(f"/api/v1/approvals/{rid}", headers=ROOT).json()
    assert full["canApprove"] and full["preview"]["diff"]["added"][0]["after"] == "31s"
    assert client.post(f"/api/v1/approvals/{rid}/_approve", headers=dev).status_code == 403
    assert client.post(f"/api/v1/approvals/{rid}/_recheck", headers=ROOT).json()["upToDate"] is True
    # approve: applied as the requester, the audit says who approved
    mail.messages.clear()
    r = client.post(f"/api/v1/approvals/{rid}/_approve", json={"comment": "ok for today"}, headers=ROOT)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "APPLIED" and out["result"]["applied"] and out["decidedBy"] == "root"
    s = es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]
    assert s["index.refresh_interval"] == "31s"
    ev = _audit(client, action="UPDATE", resource=idx, outcome="SUCCESS")
    assert ev and ev[0]["actor"] == DEV and ev[0]["approvalId"] == rid and ev[0]["approvedBy"] == "root"
    assert _audit(client, action="APPROVAL_APPROVED", approvalId=rid)
    assert [m["to"] for m in mail.messages] == [DEV] and "Approved and applied" in mail.messages[0]["subject"]
    # closed: can't approve twice; history shows it
    assert client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT).json()["error"]["code"] == "REQUEST_CLOSED"
    assert client.get("/api/v1/approvals/_count", headers=ROOT).json()["pending"] == 0
    hist = client.get(f"{API}/config-history", params={"configType": "index-settings", "resource": idx},
                      headers=ROOT).json()["items"]
    assert hist[0]["by"] == DEV
    # admins' own changes don't wait
    r = client.put(url, json={"config": {"refresh_interval": "5s"}, "reason": "admin"}, headers=ROOT)
    assert r.status_code == 200 and r.json()["applied"]


@on
def test_outdated_reject_cancel_expire(env, client, es, prefix, mail):
    idx = f"{prefix}-idx2"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0}})
    url = f"{API}/indices/{idx}/settings"
    dev = as_user(DEV)

    def ask(v):
        r = client.put(url, json={"config": {"refresh_interval": v}, "reason": "tune"}, headers=dev)
        assert r.status_code == 202, r.text
        return r.json()["approval"]["id"]

    # changed meanwhile -> OUTDATED, nothing applied
    rid = ask("40s")
    client.put(url, json={"config": {"refresh_interval": "7s"}, "reason": "hotfix"}, headers=ROOT)
    assert client.post(f"/api/v1/approvals/{rid}/_recheck", headers=ROOT).json()["upToDate"] is False
    r = client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESOURCE_CHANGED", r.text
    s = es("GET", f"/{idx}/_settings", params={"flat_settings": "true"})[idx]["settings"]
    assert s["index.refresh_interval"] == "7s"
    assert client.get(f"/api/v1/approvals/{rid}", headers=dev).json()["status"] == "OUTDATED"
    assert any("changed since you asked" in m["subject"] for m in mail.messages if m["to"] == DEV)

    # reject needs a comment and emails the requester
    rid = ask("41s")
    assert client.post(f"/api/v1/approvals/{rid}/_reject", json={}, headers=ROOT).json()["error"]["code"] == "COMMENT_REQUIRED"
    r = client.post(f"/api/v1/approvals/{rid}/_reject", json={"comment": "use 30s"}, headers=ROOT)
    assert r.json()["status"] == "REJECTED" and r.json()["comment"] == "use 30s"
    assert any("rejected" in m["subject"].lower() and m["to"] == DEV for m in mail.messages)

    # cancel: only the requester
    rid = ask("42s")
    assert client.post(f"/api/v1/approvals/{rid}/_cancel", headers=ROOT).status_code == 403
    assert client.post(f"/api/v1/approvals/{rid}/_cancel", headers=dev).json()["status"] == "CANCELLED"
    assert client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT).json()["error"]["code"] == "REQUEST_CLOSED"

    # expiry
    rid = ask("43s")
    store = env["store"]
    rec, _ = store.get_json(f"approvals/{rid}.json")
    rec["expiresAt"] = "2026-01-01T00:00:00Z"
    store.put_json(f"approvals/{rid}.json", rec)
    assert client.get(f"/api/v1/approvals/{rid}", headers=dev).json()["status"] == "EXPIRED"
    assert client.get("/api/v1/approvals/_count", headers=ROOT).json()["pending"] == 0
    statuses = {i["status"] for i in client.get("/api/v1/approvals", params={"scope": "all"}, headers=ROOT).json()["items"]}
    assert {"OUTDATED", "REJECTED", "CANCELLED", "EXPIRED"} <= statuses

    # view-only users can't even request
    r = client.put(url, json={"config": {"refresh_interval": "9s"}, "reason": "x"}, headers=as_user("looker"))
    assert r.status_code == 403


@on
def test_documents_and_bulk_through_approval(env, client, es, prefix, mail):
    idx = f"{prefix}-orders"
    es("PUT", f"/{idx}", json={"settings": {"number_of_replicas": 0},
                              "mappings": {"properties": {"status": {"type": "keyword"}}}})
    for i in range(6):
        es("PUT", f"/{idx}/_doc/o{i}", json={"status": "new", "n": i}, params={"refresh": "true"})
    dev = as_user(DEV)
    data = f"{API}/data/{idx}"

    # single edit
    doc = client.get(f"{data}/_doc/o1", headers=dev).json()
    body = {"document": {**doc["_source"], "status": "paid"}, "ifSeqNo": doc["_seq_no"],
            "ifPrimaryTerm": doc["_primary_term"], "reason": "customer paid"}
    r = client.put(f"{data}/_doc/o1", json=body, headers=dev)
    assert r.status_code == 202, r.text
    rid = r.json()["approval"]["id"]
    assert r.json()["approval"]["resource"] == f"{idx}/o1"
    assert es("GET", f"/{idx}/_doc/o1")["_source"]["status"] == "new"
    assert client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT).json()["status"] == "APPLIED"
    assert es("GET", f"/{idx}/_doc/o1")["_source"]["status"] == "paid"
    hist = client.get(f"{data}/_doc/o1/_history", headers=dev).json()["items"]
    assert hist[0]["by"] == DEV

    # delete needs the confirmation already when asking
    r = client.delete(f"{data}/_doc/o2", params={"reason": "dup"}, headers=dev)
    assert r.status_code == 400 and r.json()["error"]["code"] == "CONFIRMATION_MISMATCH"

    # bulk: dry run is mandatory before asking; approval refreshes the token
    bulk = {"query": "status:new", "set": {"status": "archived"}}
    r = client.post(f"{data}/_bulk_update", json={**bulk, "reason": "archive"}, headers=dev)
    assert r.json()["error"]["code"] == "DRY_RUN_REQUIRED"
    dry = client.post(f"{data}/_bulk_update", params={"dryRun": "true"}, json=bulk, headers=dev).json()
    assert dry["count"] == 5
    r = client.post(f"{data}/_bulk_update", json={**bulk, "reason": "archive", "dryRunToken": dry["dryRunToken"],
                                                    "expectedCount": dry["count"]}, headers=dev)
    assert r.status_code == 202, r.text
    rid = r.json()["approval"]["id"]
    assert r.json()["approval"]["summary"] == {"count": 5, "fields": ["status"], "fieldCount": 1}
    assert "archived" not in str(mail.messages[-1])
    r = client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT)
    assert r.status_code == 200 and r.json()["result"]["succeeded"] == 5, r.text
    es("POST", f"/{idx}/_refresh")
    assert es("POST", f"/{idx}/_count", json={"query": {"term": {"status": "archived"}}})["count"] == 5
    ev = _audit(client, action="DATA_BULK_UPDATE", outcome="SUCCESS", resource=idx)
    assert ev and ev[0]["actor"] == DEV and ev[0]["approvedBy"] == "root"

    # the matching set changed after the request -> outdated
    dry = client.post(f"{data}/_bulk_update", params={"dryRun": "true"},
                      json={"query": "status:archived", "set": {"status": "gone"}}, headers=dev).json()
    r = client.post(f"{data}/_bulk_update", json={"query": "status:archived", "set": {"status": "gone"},
                                                    "reason": "x", "dryRunToken": dry["dryRunToken"],
                                                    "expectedCount": dry["count"]}, headers=dev)
    rid = r.json()["approval"]["id"]
    es("PUT", f"/{idx}/_doc/o9", json={"status": "archived"}, params={"refresh": "true"})
    r = client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESOURCE_CHANGED"
    assert es("POST", f"/{idx}/_count", json={"query": {"term": {"status": "gone"}}})["count"] == 0


@on
def test_index_create_and_delete_through_approval(env, client, es, prefix, mail, open_allowlist):
    dev = as_user(DEV)
    name = f"{prefix}-new-1"
    client.put("/api/v1/admin/allowlist", json={**open_allowlist, "index-delete": {"allow": [f"{prefix}-*"]}},
               headers=ROOT)
    client.put(f"/api/v1/admin/users/{DEV}", json={"clusters": {"test": "edit"}}, headers=ROOT)
    r = client.post(f"{API}/indices/{name}", json={"settings": {"number_of_replicas": 0}, "reason": "new feed"},
                    headers=dev)
    assert r.status_code == 202, r.text
    rid = r.json()["approval"]["id"]
    assert es("GET", f"/{name}", ok=False).get("status") == 404
    assert client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT).json()["status"] == "APPLIED"
    assert name in es("GET", f"/{name}")
    # a delete request must carry the typed confirmation; then the admin approves
    r = client.delete(f"{API}/indices/{name}", params={"reason": "wrong name", "confirm": name}, headers=dev)
    assert r.status_code == 403            # needs Delete on the index, Edit is not enough
    client.put(f"/api/v1/admin/users/{DEV}", json={"clusters": {"test": {"default": "edit", "indices": [
        {"pattern": f"{prefix}-*", "level": "delete"}]}}}, headers=ROOT)
    r = client.delete(f"{API}/indices/{name}", params={"reason": "wrong name", "confirm": name}, headers=dev)
    assert r.status_code == 202, r.text
    rid = r.json()["approval"]["id"]
    r = client.post(f"/api/v1/approvals/{rid}/_approve", headers=ROOT)
    assert r.json()["status"] == "APPLIED", r.text
    assert es("GET", f"/{name}", ok=False).get("status") == 404
    tomb = client.get(f"{API}/deleted-indices", params={"index": name}, headers=ROOT).json()["items"]
    assert tomb and tomb[0]["deletedBy"] == DEV


def test_ses_mailer_sends_one_message_per_recipient():
    import boto3
    from moto import mock_aws

    from app.mailer import SesMailer, render
    with mock_aws():
        ses = boto3.client("ses", region_name="us-west-2")
        ses.verify_email_identity(EmailAddress="alerts@fenixcommerce.com")
        m = SesMailer("alerts@fenixcommerce.com", "us-west-2")
        text, html = render("Approval needed", [("Cluster", "elkm2-prod"), ("Reason", "<b>x</b>")], "intro",
                            "http://x/ui/requests/1")
        assert "&lt;b&gt;" in html and "http://x/ui/requests/1" in text
        res = m.send(["a@fenixcommerce.com", "b@fenixcommerce.com", "a@fenixcommerce.com"], "s", text, html)
        assert res == {"sent": ["a@fenixcommerce.com", "b@fenixcommerce.com"], "failed": []}
        bad = SesMailer("nobody@unverified.example", "us-west-2").send(["a@fenixcommerce.com"], "s", text, html)
        assert bad["sent"] == [] and bad["failed"][0]["to"] == "a@fenixcommerce.com"
