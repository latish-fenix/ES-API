"""Password sign-in (AUTH_MODE=password): runs against real ES + moto S3."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.auth import read_token
from app.clusters import ClusterRegistry, load_clusters
from app.main import create_app
from app.settings import Settings
from app.storage import S3Store

ADMIN = "latish.madapada@fenixcommerce.com"
ADMIN_PW = "Initial-Admin-Pass-2026"
SECRET = "x" * 40
CSRF = {"X-Requested-With": "es-config-ui"}


@pytest.fixture()
def papp(env, tmp_path):
    """A second app on the same moto S3, in password mode."""
    settings = Settings(storage_backend="s3", s3_bucket="es-config-api-test", s3_prefix="auth/",
                        clusters_file=env["app"].state.settings.clusters_file,
                        auth_mode="password", bootstrap_admins=[ADMIN],
                        bootstrap_admin_password=ADMIN_PW, session_secret=SECRET,
                        lockout_attempts=3, lockout_minutes=15, lock_ttl_seconds=60)
    store = S3Store("es-config-api-test", "auth/", client=env["s3"])
    app = create_app(settings, store=store,
                     registry=ClusterRegistry(load_clusters(settings.clusters_file)))
    return {"app": app, "store": store, "settings": settings}


def login(c: TestClient, user: str, pw: str):
    return c.post("/api/v1/auth/login", json={"username": user, "password": pw})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_requires_session_and_ignores_x_user(papp):
    c = TestClient(papp["app"])
    r = c.get("/api/v1/clusters", headers={"X-User": ADMIN})
    assert r.status_code == 401 and r.json()["error"]["code"] == "NOT_AUTHENTICATED"
    r = c.get("/api/v1/clusters", headers=bearer("esc1.bogus.token"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "SESSION_EXPIRED"


def test_bootstrap_admin_login_cookie_and_bearer(papp):
    c = TestClient(papp["app"])
    assert login(c, ADMIN, "wrong-password-123").status_code == 401
    r = login(c, ADMIN.upper(), ADMIN_PW)  # email is case-insensitive
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["admin"] and body["user"]["usingGeneratedPassword"]
    assert "esc_session" in r.cookies
    # cookie session works in the same client
    assert c.get("/api/v1/me").json()["username"] == ADMIN
    # bearer works without the cookie
    fresh = TestClient(papp["app"])
    me = fresh.get("/api/v1/me", headers=bearer(body["token"])).json()
    assert me["admin"] is True and me["authMode"] == "password"
    # the hash never leaves the API
    users = fresh.get("/api/v1/admin/users", headers=bearer(body["token"])).json()["items"]
    assert all("passwordHash" not in u for u in users)


def test_cookie_writes_need_csrf_header(papp):
    c = TestClient(papp["app"])
    login(c, ADMIN, ADMIN_PW)
    body = {"username": "a@fenixcommerce.com", "clusters": {"test": "view"}}
    r = c.post("/api/v1/admin/users", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_CHECK_FAILED"
    assert c.post("/api/v1/admin/users", json=body, headers=CSRF).status_code == 201


def test_create_user_generated_password_csv_and_change(papp):
    admin = TestClient(papp["app"])
    tok = login(admin, ADMIN, ADMIN_PW).json()["token"]
    H = bearer(tok)

    r = admin.post("/api/v1/admin/users", headers=H,
                   json={"username": "Priya@FenixCommerce.com", "clusters": {"test": "edit"}})
    assert r.status_code == 201, r.text
    cred = r.json()["credentials"]
    assert cred["username"] == "priya@fenixcommerce.com" and len(cred["password"]) == 16

    # not an email -> refused; duplicate -> 409
    r = admin.post("/api/v1/admin/users", headers=H, json={"username": "priya"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_EMAIL"
    r = admin.post("/api/v1/admin/users", headers=H, json={"username": "priya@fenixcommerce.com"})
    assert r.status_code == 409

    # CSV download
    r = admin.post("/api/v1/admin/users?format=csv", headers=H,
                   json={"username": "sam@fenixcommerce.com", "clusters": {"*": "view"}})
    assert r.status_code == 201 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["username"] == "sam@fenixcommerce.com" and len(rows[0]["password"]) == 16
    sam_pw = rows[0]["password"]

    # user signs in with the generated password and changes it
    u = TestClient(papp["app"])
    t1 = login(u, "priya@fenixcommerce.com", cred["password"]).json()["token"]
    assert u.get("/api/v1/me", headers=bearer(t1)).json()["usingGeneratedPassword"] is True
    r = u.post("/api/v1/auth/change-password", headers=bearer(t1),
               json={"currentPassword": cred["password"], "newPassword": "short"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "WEAK_PASSWORD"
    r = u.post("/api/v1/auth/change-password", headers=bearer(t1),
               json={"currentPassword": "wrong", "newPassword": "Priya-New-Pass-99"})
    assert r.status_code == 401
    r = u.post("/api/v1/auth/change-password", headers=bearer(t1),
               json={"currentPassword": cred["password"], "newPassword": "Priya-New-Pass-99"})
    assert r.status_code == 200, r.text
    t2 = r.json()["token"]
    # old token dead, new token works, old password dead
    assert u.get("/api/v1/me", headers=bearer(t1)).status_code == 401
    assert u.get("/api/v1/me", headers=bearer(t2)).json()["usingGeneratedPassword"] is False
    assert login(TestClient(papp["app"]), "priya@fenixcommerce.com", cred["password"]).status_code == 401
    assert login(TestClient(papp["app"]), "priya@fenixcommerce.com", "Priya-New-Pass-99").status_code == 200

    # permissions still enforced: priya has edit on test only
    assert u.get("/api/v1/admin/users", headers=bearer(t2)).status_code == 403
    assert login(TestClient(papp["app"]), "sam@fenixcommerce.com", sam_pw).status_code == 200


def test_admin_reset_password_ends_sessions(papp):
    admin = TestClient(papp["app"])
    H = bearer(login(admin, ADMIN, ADMIN_PW).json()["token"])
    pw = admin.post("/api/v1/admin/users", headers=H,
                    json={"username": "reset@fenixcommerce.com"}).json()["credentials"]["password"]
    ut = login(TestClient(papp["app"]), "reset@fenixcommerce.com", pw).json()["token"]
    r = admin.post("/api/v1/admin/users/reset@fenixcommerce.com/reset-password?format=csv", headers=H)
    assert r.status_code == 200 and r.text.startswith("username,password")
    new_pw = r.text.strip().split("\r\n")[1].split(",", 1)[1]
    assert TestClient(papp["app"]).get("/api/v1/me", headers=bearer(ut)).status_code == 401
    assert login(TestClient(papp["app"]), "reset@fenixcommerce.com", pw).status_code == 401
    assert login(TestClient(papp["app"]), "reset@fenixcommerce.com", new_pw).status_code == 200


def test_bulk_create_all_or_nothing(papp):
    admin = TestClient(papp["app"])
    H = bearer(login(admin, ADMIN, ADMIN_PW).json()["token"])
    r = admin.post("/api/v1/admin/users/bulk?format=csv", headers=H, json={"users": [
        {"username": "b1@fenixcommerce.com", "clusters": {"test": "view"}},
        {"username": "b2@fenixcommerce.com", "clusters": {"test": "edit"}}]})
    assert r.status_code == 201
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["username"] for x in rows] == ["b1@fenixcommerce.com", "b2@fenixcommerce.com"]
    r = admin.post("/api/v1/admin/users/bulk", headers=H, json={"users": [
        {"username": "b3@fenixcommerce.com"}, {"username": "b1@fenixcommerce.com"}]})
    assert r.status_code == 409 and r.json()["error"]["details"]["existing"] == ["b1@fenixcommerce.com"]
    assert admin.get("/api/v1/admin/users/b3@fenixcommerce.com", headers=H).status_code == 404


def test_lockout_after_failed_attempts(papp):
    admin = TestClient(papp["app"])
    H = bearer(login(admin, ADMIN, ADMIN_PW).json()["token"])
    pw = admin.post("/api/v1/admin/users", headers=H,
                    json={"username": "lock@fenixcommerce.com"}).json()["credentials"]["password"]
    c = TestClient(papp["app"])
    for _ in range(3):
        assert login(c, "lock@fenixcommerce.com", "nope-nope-nope").status_code == 401
    r = login(c, "lock@fenixcommerce.com", pw)  # right password, but locked
    assert r.status_code == 423 and r.json()["error"]["code"] == "ACCOUNT_LOCKED"
    # an admin reset unlocks
    new_pw = admin.post("/api/v1/admin/users/lock@fenixcommerce.com/reset-password",
                        headers=H).json()["credentials"]["password"]
    assert login(c, "lock@fenixcommerce.com", new_pw).status_code == 200
    # unknown users get the same answer as a wrong password
    r = login(c, "nobody@fenixcommerce.com", "whatever-123")
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_logout_and_logout_all(papp):
    c = TestClient(papp["app"])
    t = login(c, ADMIN, ADMIN_PW).json()["token"]
    assert c.post("/api/v1/auth/logout").status_code == 200
    assert c.get("/api/v1/me").status_code == 401  # cookie gone
    assert c.get("/api/v1/me", headers=bearer(t)).status_code == 200  # token still valid
    assert c.post("/api/v1/auth/logout-all", headers=bearer(t)).status_code == 200
    assert c.get("/api/v1/me", headers=bearer(t)).status_code == 401


def test_audit_records_auth_events_without_passwords(papp):
    c = TestClient(papp["app"])
    login(c, ADMIN, "bad-password-1")
    t = login(c, ADMIN, ADMIN_PW).json()["token"]
    today = datetime.now(timezone.utc).date().isoformat()
    ev = c.get(f"/api/v1/admin/audit?date={today}&action=AUTH_LOGIN", headers=bearer(t)).json()["items"]
    outcomes = {e["outcome"] for e in ev}
    assert {"SUCCESS", "REJECTED"} <= outcomes
    raw = str(ev)
    assert ADMIN_PW not in raw and "bad-password-1" not in raw


def test_config_api_works_with_session(papp, prefix):
    c = TestClient(papp["app"])
    H = bearer(login(c, ADMIN, ADMIN_PW).json()["token"])
    r = c.put("/api/v1/clusters/test/cluster-settings?dryRun=true", headers=H,
              json={"config": {"indices.recovery.max_bytes_per_sec": "41mb"}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_ALLOWLISTED"  # auth ok, allowlist empty


def test_token_tamper_rejected(papp):
    c = TestClient(papp["app"])
    t = login(c, ADMIN, ADMIN_PW).json()["token"]
    assert read_token(SECRET, t)["sub"] == ADMIN
    head, payload, sig = t.split(".")
    forged = f"{head}.{payload[:-2]}AA.{sig}"
    assert TestClient(papp["app"]).get("/api/v1/me", headers=bearer(forged)).status_code == 401
    assert read_token("y" * 40, t) is None


def test_first_start_puts_every_secret_in_secrets_manager(env):
    """No SESSION_SECRET / BOOTSTRAP_ADMIN_PASSWORD anywhere: both are generated into the
    app secret; the admin signs in with the generated password; S3 never holds a hash."""
    import json as _json
    from app.secret_store import AwsSecretStore
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    s = Settings(storage_backend="s3", s3_bucket="es-config-api-test", s3_prefix="auth2/",
                 clusters_file=env["app"].state.settings.clusters_file, auth_mode="password",
                 bootstrap_admins=["boss@fenixcommerce.com"], secrets_prefix="t2/")
    store = S3Store("es-config-api-test", "auth2/", client=env["s3"])
    app = create_app(s, store=store, registry=ClusterRegistry(load_clusters(s.clusters_file)),
                     secrets=AwsSecretStore("t2/", client=sm))
    appsec = _json.loads(sm.get_secret_value(SecretId="t2/app")["SecretString"])
    assert len(appsec["sessionSecret"]) >= 32 and appsec["bootstrapAdminPassword"]
    c = TestClient(app)
    r = login(c, "boss@fenixcommerce.com", appsec["bootstrapAdminPassword"])
    assert r.status_code == 200, r.text
    assert read_token(appsec["sessionSecret"], r.json()["token"])["sub"] == "boss@fenixcommerce.com"
    users, _ = store.get_json("state/users.json")
    assert "passwordHash" not in _json.dumps(users) and users["users"]["boss@fenixcommerce.com"]["hasPassword"]
    h = _json.loads(sm.get_secret_value(SecretId="t2/users/boss@fenixcommerce.com")["SecretString"])
    assert h["passwordHash"].startswith("scrypt$")


def test_legacy_hashes_in_s3_move_to_secrets_manager(env):
    """An older deployment kept hashes in users.json: the next start moves them."""
    import json as _json
    from app.auth import hash_password
    from app.secret_store import AwsSecretStore
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    store = S3Store("es-config-api-test", "auth3/", client=env["s3"])
    store.put_json("state/users.json", {"users": {"old@fenixcommerce.com": {
        "admin": True, "clusters": {}, "passwordHash": hash_password("Old-Password-2026"),
        "tokenVersion": 1}}})
    s = Settings(storage_backend="s3", s3_bucket="es-config-api-test", s3_prefix="auth3/",
                 clusters_file=env["app"].state.settings.clusters_file, auth_mode="password",
                 session_secret=SECRET, secrets_prefix="t3/")
    app = create_app(s, store=store, registry=ClusterRegistry(load_clusters(s.clusters_file)),
                     secrets=AwsSecretStore("t3/", client=sm))
    users, _ = store.get_json("state/users.json")
    assert "passwordHash" not in _json.dumps(users)
    assert login(TestClient(app), "old@fenixcommerce.com", "Old-Password-2026").status_code == 200


def test_auth_config_is_public(papp):
    c = TestClient(papp["app"])
    r = c.get("/api/v1/auth/config")
    assert r.status_code == 200
    assert r.json() == {"authMode": "password", "sessionHours": 12, "lockoutAttempts": 3,
                        "lockoutMinutes": 15}


def test_ui_is_served_with_security_headers(papp):
    from app.main import UI_DIR
    if not (UI_DIR / "index.html").exists():
        pytest.skip("UI not built (cd ui && npm ci && npm run build)")
    c = TestClient(papp["app"])
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"
    for path in ("/ui/", "/ui/c/elkm2-prod/indices", "/ui/login"):  # SPA fallback
        r = c.get(path)
        assert r.status_code == 200 and "<div id=\"root\">" in r.text, path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-cache"
    asset = next((UI_DIR / "assets").glob("*.js")).name
    r = c.get(f"/ui/assets/{asset}")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert c.get("/ui/../app/main.py").status_code in (200, 404)  # never the source file
    assert "create_app" not in c.get("/ui/..%2Fmain.py").text
    assert c.get("/api/v1/clusters").headers["x-content-type-options"] == "nosniff"


def test_workers_starting_together_share_the_first_secrets(env):
    """uvicorn starts two workers at once: on a fresh install they must end up with the same
    session key and one first-admin password that really works (a startup lock in S3)."""
    import json as _json
    import threading
    from app.secret_store import AwsSecretStore
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    s = Settings(storage_backend="s3", s3_bucket="es-config-api-test", s3_prefix="auth4/",
                 clusters_file=env["app"].state.settings.clusters_file, auth_mode="password",
                 bootstrap_admins=["boss@fenixcommerce.com"], secrets_prefix="t4/")
    apps, errors = [], []

    def start():
        try:
            apps.append(create_app(s, store=S3Store("es-config-api-test", "auth4/", client=env["s3"]),
                                   registry=ClusterRegistry(load_clusters(s.clusters_file)),
                                   secrets=AwsSecretStore("t4/", client=sm)))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(apps) == 3
    appsec = _json.loads(sm.get_secret_value(SecretId="t4/app")["SecretString"])
    assert {a.state.settings.session_secret for a in apps} == {appsec["sessionSecret"]}
    for a in apps:
        r = login(TestClient(a), "boss@fenixcommerce.com", appsec["bootstrapAdminPassword"])
        assert r.status_code == 200, r.text
    # the startup lock is released
    assert S3Store("es-config-api-test", "auth4/", client=env["s3"]).get_json("locks/_app/startup/init.lock")[0] is None
