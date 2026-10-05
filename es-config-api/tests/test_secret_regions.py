"""Moving secrets between regions: new ones in the main region, old ones read from the legacy
region, moved on update (legacy copy scheduled for deletion), and migrate-secrets for the rest."""
from __future__ import annotations

import json

import boto3
from moto import mock_aws

from app.auth import hash_password
from app.repos import UsersRepo
from app.secret_store import AwsSecretStore
from app.storage import LocalStore


def _stores():
    west = boto3.client("secretsmanager", region_name="us-west-2")
    east = boto3.client("secretsmanager", region_name="us-east-1")
    store = AwsSecretStore("r/", region="us-west-2", client=west, legacy_region="us-east-1",
                           legacy_client=east, cache_seconds=0)
    return west, east, store


def _deleted(sm, name):
    return bool(sm.describe_secret(SecretId=name).get("DeletedDate"))


def test_read_fallback_and_move_on_update():
    with mock_aws():
        west, east, store = _stores()
        east.create_secret(Name="r/clusters/old", SecretString=json.dumps({"password": "p1"}),
                           Description="ES Config API: credentials for cluster old")
        # read falls back to the legacy region
        assert store.get("clusters/old") == {"password": "p1"}
        assert store.where("clusters/old") == "us-east-1"
        # new secrets go to the main region only
        store.put("clusters/new", {"password": "n1"})
        assert json.loads(west.get_secret_value(SecretId="r/clusters/new")["SecretString"]) == {"password": "n1"}
        assert store.where("clusters/new") == "us-west-2"
        assert not any(s["Name"] == "r/clusters/new" for s in east.list_secrets()["SecretList"])
        # an update moves it: new value in west, east copy scheduled for deletion (recoverable)
        store.put("clusters/old", {"password": "p2"})
        assert json.loads(west.get_secret_value(SecretId="r/clusters/old")["SecretString"]) == {"password": "p2"}
        assert _deleted(east, "r/clusters/old")
        assert store.get("clusters/old") == {"password": "p2"} and store.where("clusters/old") == "us-west-2"
        east.restore_secret(SecretId="r/clusters/old")            # still recoverable
        assert store.get("clusters/old") == {"password": "p2"}    # main region wins
        # a second update with the east copy back: retired again, no error
        store.put("clusters/old", {"password": "p3"})
        assert _deleted(east, "r/clusters/old")
        # delete removes both
        east.create_secret(Name="r/users/x", SecretString="{}")
        store.put("users/x", {"passwordHash": "h"})
        store.delete("users/x")
        assert store.get("users/x") is None


def test_migrate_moves_the_rest():
    with mock_aws():
        west, east, store = _stores()
        east.create_secret(Name="r/app", SecretString=json.dumps({"sessionSecret": "s" * 64}))
        east.create_secret(Name="r/users/a@x.com", SecretString=json.dumps({"passwordHash": "h"}))
        west.create_secret(Name="r/users/b@x.com", SecretString=json.dumps({"passwordHash": "hb"}))
        east.create_secret(Name="r/users/b@x.com", SecretString=json.dumps({"passwordHash": "old"}))
        assert store.migrate("app", dry_run=True) == "moved"
        assert not _deleted(east, "r/app")                       # dry run changed nothing
        assert store.migrate("app", dry_run=False) == "moved"
        assert json.loads(west.get_secret_value(SecretId="r/app")["SecretString"]) == {"sessionSecret": "s" * 64}
        assert _deleted(east, "r/app")
        assert store.migrate("users/a@x.com", dry_run=False) == "moved"
        # already in west: the stale east copy is only retired, the west value kept
        assert store.migrate("users/b@x.com", dry_run=False) == "retired"
        assert json.loads(west.get_secret_value(SecretId="r/users/b@x.com")["SecretString"])["passwordHash"] == "hb"
        assert store.migrate("users/b@x.com", dry_run=False) == "in-main"
        assert store.migrate("clusters/none", dry_run=False) == "missing"


def test_password_change_moves_the_user_secret(tmp_path):
    with mock_aws():
        west, east, store = _stores()
        east.create_secret(Name="r/users/dev@x.com", SecretString=json.dumps({"passwordHash": hash_password("Old-Pass-2026x")}))
        users = UsersRepo(LocalStore(str(tmp_path)), ["dev@x.com"], store)
        assert users.password_hash("dev@x.com")                   # read from us-east-1
        users.set_password("dev@x.com", hash_password("New-Pass-2026x"), False, "dev@x.com")
        assert store.where("users/dev@x.com") == "us-west-2"
        assert _deleted(east, "r/users/dev@x.com")


def test_no_access_to_the_new_region_falls_back_for_reads():
    from botocore.exceptions import ClientError

    from app.errors import ApiError

    class Denied:
        meta = type("M", (), {"region_name": "us-west-2"})()

        def get_secret_value(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no identity-based policy allows"}},
                              "GetSecretValue")

        put_secret_value = get_secret_value

    with mock_aws():
        east = boto3.client("secretsmanager", region_name="us-east-1")
        east.create_secret(Name="r/app", SecretString=json.dumps({"sessionSecret": "s" * 64}))
        store = AwsSecretStore("r/", region="us-west-2", client=Denied(), legacy_region="us-east-1",
                               legacy_client=east, cache_seconds=0)
        assert store.get("app") == {"sessionSecret": "s" * 64}          # still starts and signs in
        assert store.get("users/none") is None
        try:
            store.put("app", {"sessionSecret": "x"})
            raise AssertionError("write should fail")
        except ApiError as e:
            assert e.code == "SECRETS_ACCESS_DENIED" and "us-west-2" in e.message
        nolegacy = AwsSecretStore("r/", region="us-west-2", client=Denied(), cache_seconds=0)
        try:
            nolegacy.get("app")
            raise AssertionError("should fail without a legacy region")
        except ApiError as e:
            assert e.details["region"] == "us-west-2" and "identity-based" in e.details["awsMessage"]
