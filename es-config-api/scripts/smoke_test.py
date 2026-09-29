"""End-to-end smoke test against a RUNNING API (real ES + real storage).

    python scripts/smoke_test.py --api http://localhost:8080 --admin you@company.com \
        --admin-password '...' --cluster local \
        --es-url http://127.0.0.1:9200 --es-user elastic --es-password ...

It changes one harmless cluster setting (indices.recovery.max_bytes_per_sec) and creates
one index template named smoke-<random>; both are cleaned up at the end. It REPLACES the
global allowlist, so only run it against a test storage prefix.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone

import requests

KEY = "indices.recovery.max_bytes_per_sec"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--admin", required=True, help="an admin's email (e.g. a BOOTSTRAP_ADMINS user)")
    ap.add_argument("--admin-password", default=None, help="password mode: the admin's password")
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--es-url", required=True, help="direct ES access, to simulate drift")
    ap.add_argument("--es-user", default="elastic")
    ap.add_argument("--es-password", required=True)
    a = ap.parse_args()

    api = a.api.rstrip("/") + "/api/v1"
    tester_name = "smoke-tester@example.com"
    if a.admin_password:  # AUTH_MODE=password
        r = requests.post(f"{api}/auth/login", json={"username": a.admin, "password": a.admin_password})
        if not r.ok:
            print("admin login failed:", r.text)
            return 1
        admin = {"Authorization": f"Bearer {r.json()['token']}"}
    else:  # AUTH_MODE=header
        admin = {"X-User": a.admin}
    tester = None  # set after the tester user is created
    c = f"{api}/clusters/{a.cluster}"
    tpl = f"smoke-{uuid.uuid4().hex[:6]}"

    def es(method, path, **kw):
        return requests.request(method, a.es_url + path, auth=(a.es_user, a.es_password), timeout=30, **kw)

    s = requests.Session()
    try:
        r = s.get(a.api.rstrip("/") + "/healthz")
        check("healthz", r.ok, r.text)

        r = s.get(f"{api}/admin/clusters", headers=admin)
        item = next((i for i in r.json().get("items", []) if i["id"] == a.cluster), {})
        check("admin sees cluster and it is reachable", r.ok and item.get("reachable"), r.text)

        r = s.put(f"{api}/admin/allowlist", headers=admin, json={
            "cluster-settings": {"allow": ["indices.recovery.*"]},
            "index-templates": {"allow": ["smoke-*"], "indexPatterns": ["smoke-*"]}})
        check("set allowlist", r.ok, r.text)

        r = s.post(f"{api}/admin/users", headers=admin,
                   json={"username": tester_name, "clusters": {a.cluster: "view"}})
        check("create view-only user", r.ok, r.text)
        if a.admin_password:
            pw = r.json()["credentials"]["password"]
            check("generated password returned once", bool(pw) and len(pw) == 16, r.text)
            lr = s.post(f"{api}/auth/login", json={"username": tester_name, "password": pw})
            check("new user can sign in", lr.ok, lr.text)
            tester = {"Authorization": f"Bearer {lr.json().get('token', '')}"}
        else:
            tester = {"X-User": tester_name}
        r = s.put(f"{c}/cluster-settings", headers=tester, json={"config": {KEY: "71mb"}, "reason": "x"})
        check("view-only user cannot change (403)", r.status_code == 403, r.text)
        s.put(f"{api}/admin/users/{tester_name}/permissions", headers=admin, json={a.cluster: "edit"})

        g = s.get(f"{c}/cluster-settings", headers=tester)
        check("get cluster settings + ETag", g.ok and "ETag" in g.headers, g.text)

        r = s.put(f"{c}/cluster-settings?dryRun=true", headers=tester, json={"config": {KEY: "71mb"}})
        d = r.json()
        check("dry run shows diff, applies nothing", r.ok and not d.get("applied")
              and any(x["path"] == KEY for part in d["diff"].values() for x in part), r.text)

        r = s.put(f"{c}/cluster-settings", headers={**tester, "If-Match": '"0000000000000000"'},
                  json={"config": {KEY: "71mb"}, "reason": "smoke"})
        check("stale If-Match rejected (412)", r.status_code == 412, r.text)

        r = s.put(f"{c}/cluster-settings", headers={**tester, "If-Match": g.headers.get("ETag", "")},
                  json={"config": {KEY: "71mb"}, "reason": "smoke test update"})
        check("update applied", r.ok and r.json().get("applied"), r.text)
        live = es("GET", "/_cluster/settings", params={"flat_settings": "true"}).json()["persistent"]
        check("ES really has the new value", live.get(KEY) == "71mb", str(live))

        r = s.get(f"{c}/cluster-settings/previous", headers=tester)
        check("snapshot stored in storage", r.ok and "state" in r.json(), r.text)

        r = s.post(f"{c}/cluster-settings/rollback?dryRun=true", headers=tester, json={})
        check("rollback dry run", r.ok and not r.json().get("applied"), r.text)
        r = s.post(f"{c}/cluster-settings/rollback", headers=tester, json={"reason": "smoke rollback"})
        check("rollback applied", r.ok and r.json().get("applied"), r.text)
        live = es("GET", "/_cluster/settings", params={"flat_settings": "true"}).json()["persistent"]
        check("ES back to the old value", live.get(KEY) != "71mb", str(live))

        # drift: change ES behind the API's back
        s.put(f"{c}/cluster-settings", headers=tester, json={"config": {KEY: "72mb"}, "reason": "smoke"})
        es("PUT", "/_cluster/settings", json={"persistent": {KEY: "99mb"}})
        r = s.get(f"{c}/cluster-settings", headers=tester)
        check("drift detected on GET", r.ok and r.json().get("driftDetected") is True, r.text)
        r = s.post(f"{c}/cluster-settings/rollback", headers=tester, json={"reason": "smoke"})
        check("rollback refused while drifted (409)", r.status_code == 409, r.text)

        # index template: create, then rollback deletes it
        body = {"config": {"index_patterns": [f"{tpl}-*"], "priority": 7}, "reason": "smoke template"}
        r = s.put(f"{c}/index-templates/{tpl}?dryRun=true", headers=tester, json=body)
        check("template dry run has ES simulation", r.ok and r.json().get("simulation"), r.text)
        r = s.put(f"{c}/index-templates/{tpl}", headers=tester, json=body)
        check("template created", r.ok and r.json().get("createsResource"), r.text)
        r = s.post(f"{c}/index-templates/{tpl}/rollback", headers=tester, json={"reason": "smoke"})
        check("template rollback deletes it", r.ok and r.json().get("deletesResource"), r.text)

        today = datetime.now(timezone.utc).date().isoformat()
        r = s.get(f"{api}/admin/audit", headers=admin,
                  params={"date": today, "clusterId": a.cluster, "user": tester_name})
        acts = {e["action"] for e in r.json().get("items", [])}
        check("audit log has UPDATE, ROLLBACK, DRY_RUN", {"UPDATE", "ROLLBACK", "DRY_RUN"} <= acts, str(acts))
    finally:
        es("PUT", "/_cluster/settings", json={"persistent": {KEY: None}})
        es("DELETE", f"/_index_template/{tpl}")
        s.delete(f"{api}/admin/users/{tester_name}", headers=admin)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
