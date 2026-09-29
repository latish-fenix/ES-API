# Test the ES Config API on your Windows PC

About 15 minutes the first time. Everything runs on your PC: Elasticsearch 8.17.1 on
port 9200 and the API on port 8080. Nothing is sent to AWS unless you choose the S3 option.

## 1. Start everything

Open **Command Prompt** in `Desktop\ES-API\es-config-api\local-test` and run:

| Step | Command | What it does |
| --- | --- | --- |
| 1 | `1-start-elasticsearch.cmd` | Installs Elasticsearch 8.17.1 into `%USERPROFILE%\es-local` on the first run (a ~480 MB download, checksum verified), starts it in a minimized window, and creates the `config_api` service account, a read-only `reader` user and some demo data |
| 2 | `2-start-api.cmd` | On the first run, creates a Python environment (it installs Python 3.12 with winget if needed), installs the packages and starts the API. Leave this window open; press Ctrl+C to stop |

By default the API stores its state in local JSON files under `local-test\.store`. To use
S3 instead, run `2-start-api-s3.cmd`. It uses your `fenix-prod` profile and
`s3://fenix-ecr-logs/cron-migration/`.

Then open **http://localhost:8080/ui/** and sign in as
`latish.madapada@fenixcommerce.com` with the password `Local-Test-Admin-2026`. Section 1a
below is a 15-minute tour of the web console. Sections 2 and 3 test the same things through
the raw API at **http://localhost:8080/docs** (click an endpoint, **Try it out**, **Execute**).

## 1a. Tour of the web console

`docs/USER_GUIDE.md` explains every screen in more detail, with screenshots.

| # | Where | Do | Expect |
| --- | --- | --- | --- |
| 1 | **Allowlist** | Unlock *Cluster settings*, add `indices.recovery.*`; unlock *Index mappings*, add `products-*`; unlock *Index delete*, add `products-demo`; unlock *ILM policies* and *Ingest pipelines*, add `demo-*` to each; **Save allowlist** | "Global allowlist saved" |
| 2 | **Cluster settings** | In the editor type `{"indices.recovery.max_bytes_per_sec": "80mb"}`, click **Dry run** | Green "Dry run passed", a diff row *Added … default → 80mb*. Nothing changed yet |
| 3 | same | Enter a reason, **Apply change** | The setting appears on the left; "Snapshot saved" badge |
| 4 | same | **Roll back**, enter a reason, **Roll back** | The setting is gone. Roll back again: it's back (rollback swaps) |
| 5 | same | Dry run `{"cluster.routing.allocation.enable": "none"}` | Red "Not on the allowlist" |
| 6 | **Indices** → `products-demo` → **Mapping** | Dry run `{"properties": {"brand": {"type": "keyword"}}}`, reason, tick "I understand", **Add fields permanently** | `brand` is listed under Current fields |
| 7 | **ILM policies** → `demo-logs-policy` | Change `7d` to `3d`, Dry run, apply, then **View snapshot** and **Roll back** | Diff shows 7d → 3d; rollback restores 7d |
| 8 | **Ingest pipelines** → `demo-pipeline` | Dry run with the sample document box | "Result for each sample document" shows the processed doc |
| 9 | **Users** | **Add users**: `alice@example.com`, access *Edit* → **Create**, **Download CSV** | A CSV with `username,password` |
| 10 | Sign out, sign in as alice | Open **Cluster settings** | She can edit; there is no Administration menu |
| 11 | As alice: **Change password** (bottom left) | Set a new password | "Password changed" |
| 12 | Sign in as the admin: **Audit log** | Expand a *Rejected* row | Error code, message, blocked keys, request id |
| 13 | **Indices** → `products-demo` → **Delete** | Type the name and a reason | The index is gone; see it under "Deleted through the API" |
| 14 | **Clusters** → **Add cluster** | Id `local-copy`, node URL `http://127.0.0.1:9200`, username `config_api`, password `wrong`; **Test connection** | Red "Can't connect", with the Elasticsearch authentication error |
| 15 | same | Password `svc-pass-123`, **Test connection**, then **Add cluster** | Green "Connected · Elasticsearch 8.17.1"; the row shows **In console**; `local-copy` is in the cluster switcher; `local-test\clusters.managed.yaml` now holds it (not the `.store` / S3 folder) |
| 16 | same | **Edit** `local-copy`, change the display name, leave the password empty, **Save**; then **Details** on `local` | Saved, still connects (password kept); `local` is read-only ("Defined in config/clusters.yaml") |
| 17 | same | **Edit** `local-copy` → **Remove cluster**, type `local-copy`, confirm | Gone from the list and the switcher; the audit log shows `ADMIN_CLUSTER_CREATE`, `_UPDATE`, `_DELETE` |
| 18 | **Data** (or **Indices** → `products-demo` → **Browse**) | Index `products-demo`; query blank, **Search**; click a column heading; **Add filter** on any field; click a row | A document count, sorted rows, the filter as a chip, the document's fields and JSON |
| 19 | same | **Export** → CSV → **Download**; then type `.security*` as the index and **Search** | A CSV opens in Excel; the system index is refused ("System and hidden indices … can't be browsed"). The audit log shows `DATA_SEARCH` and `DATA_EXPORT` |

Steps 18–19 need the `config_api` account to have the `read` privilege. If you set up
Elasticsearch before the data browser existed, run `1-start-elasticsearch.cmd` again (it's safe
to repeat) to update the role.

**Sign in.** In /docs, open `POST /api/v1/auth/login`, click **Try it out**, send
`{"username": "latish.madapada@fenixcommerce.com", "password": "Local-Test-Admin-2026"}`
and copy the `token` from the response. Click **Authorize** (top right), paste the token and
click **Authorize**. To act as another user, sign in as them the same way and paste their token.

**Creating users** (steps 2.3 and 2.4) returns each user's generated password once in the
response (add `?format=csv` to download it as a CSV instead). Write the passwords down: you
sign in as alice and bob with them in step 3.

## 2. Admin setup (signed in as the admin)

| # | Endpoint | Body | Expect |
| --- | --- | --- | --- |
| 2.1 | `GET /api/v1/admin/clusters` | — | `local`, `"reachable": true`, version `8.17.1` |
| 2.2 | `PUT /api/v1/admin/allowlist` | the allowlist below | 200 |
| 2.3 | `POST /api/v1/admin/users` | `{"username": "alice@example.com", "clusters": {"local": "edit"}}` | 201, `credentials.password` |
| 2.4 | `POST /api/v1/admin/users` | `{"username": "bob@example.com", "clusters": {"local": "view"}}` | 201, `credentials.password` |

The allowlist for 2.2:

```json
{
  "cluster-settings":    {"allow": ["indices.recovery.*", "cluster.routing.allocation.*"],
                          "deny":  ["cluster.routing.allocation.enable"]},
  "index-settings":      {"allow": ["index.refresh_interval", "index.number_of_replicas"],
                          "indices": ["products-*"]},
  "index-mappings":      {"allow": ["products-*"]},
  "index-templates":     {"allow": ["demo-*"], "indexPatterns": ["demo-*"]},
  "component-templates": {"allow": ["demo-*"]},
  "ilm-policies":        {"allow": ["demo-*"]},
  "ingest-pipelines":    {"allow": ["demo-*"]}
}
```

## 3. Try it out

Switch to **alice**: sign in as `alice@example.com` with her password from 2.3, then Authorize with the new token. Use `local` as `cluster_id` everywhere.

### Cluster settings: update, rollback, swap

| # | Endpoint | Body / params | Expect |
| --- | --- | --- | --- |
| 3.1 | `GET /api/v1/clusters` | — | only `local`, permission `edit` |
| 3.2 | `GET …/cluster-settings` | — | current settings and a `version` (keep it for 3.9) |
| 3.3 | `PUT …/cluster-settings`, `dryRun=true` | `{"config": {"indices.recovery.max_bytes_per_sec": "80mb"}}` | `applied: false`, `diff.added` shows the key. Nothing changes |
| 3.4 | same, `dryRun=false` | same body, but without a `reason` | 400 `REASON_REQUIRED` |
| 3.5 | same | add `"reason": "testing"` | 200, `applied: true` |
| 3.6 | `GET …/cluster-settings/previous` | — | the snapshot from before 3.5 |
| 3.7 | `POST …/cluster-settings/rollback` | `{"reason": "undo"}` | 200; the key is gone again |
| 3.8 | the same rollback again | `{"reason": "redo"}` | 200; `80mb` is back (rollback swaps) |
| 3.9 | `PUT …/cluster-settings` with `If-Match` set to the old version from 3.2 | any change + reason | 412 `VERSION_MISMATCH` |
| 3.10 | `PUT …/cluster-settings` | `{"config": {"cluster.routing.allocation.enable": "none"}, "reason": "x"}` | 403 `NOT_ALLOWLISTED` (denied key) |

### Drift: a change made outside the API

In Command Prompt, change the setting directly as the `elastic` superuser:

```cmd
curl.exe -u elastic:changeme123 -X PUT http://127.0.0.1:9200/_cluster/settings -H "Content-Type: application/json" -d "{\"persistent\":{\"indices.recovery.max_bytes_per_sec\":\"99mb\"}}"
```

| # | Endpoint | Expect |
| --- | --- | --- |
| 3.11 | `GET …/cluster-settings` | `"driftDetected": true` |
| 3.12 | `POST …/cluster-settings/rollback` with `{"reason": "x"}` | 409 `DRIFT_DETECTED` |
| 3.13 | the same, with `force=true` | 200 |

The read-only `reader` user can't change settings directly:

```cmd
curl.exe -u reader:reader-pass -X PUT http://127.0.0.1:9200/_cluster/settings -H "Content-Type: application/json" -d "{\"persistent\":{\"indices.recovery.max_bytes_per_sec\":\"10mb\"}}"
```

This returns `security_exception`, which is how the lockdown is meant to work.

### Index settings and mappings (`index` = `products-demo`)

| # | Endpoint | Body | Expect |
| --- | --- | --- | --- |
| 3.14 | `PUT …/indices/{index}/{part}` with part `settings` | `{"config": {"refresh_interval": "30s"}, "reason": "t"}` | 200, then roll back with `POST …/settings/rollback` |
| 3.15 | same | `{"config": {"number_of_shards": 3}, "reason": "t"}` | 422 `STATIC_SETTING` |
| 3.16 | part `mapping`, `dryRun=true` | `{"config": {"properties": {"brand": {"type": "keyword"}}}}` | `permanent: true`, with a warning that it can't be rolled back |
| 3.17 | part `mapping` | same body + `"reason": "t"` | 200; the field `brand` is added |
| 3.18 | part `mapping` | `{"config": {"properties": {"sku": {"type": "long"}}}, "reason": "t"}` | 422 `MAPPING_CONFLICT` |
| 3.19 | `POST …/mapping/rollback` | `{"reason": "t"}` | 422 `ROLLBACK_NOT_SUPPORTED` |

### Templates, ILM policies and pipelines

| # | Endpoint | Body | Expect |
| --- | --- | --- | --- |
| 3.20 | `PUT …/{config_type}/{name}`, `index-templates` / `demo-tpl`, `dryRun=true` | `{"config": {"index_patterns": ["demo-*"], "priority": 50, "template": {"settings": {"number_of_replicas": 0}}}}` | `createsResource: true`, and `simulation` shows the resolved settings |
| 3.21 | same, real | add `"reason": "t"` | 200 |
| 3.22 | `POST …/index-templates/demo-tpl/rollback` | `{"reason": "t"}` | 200, `deletesResource: true` (it didn't exist before) |
| 3.23 | `index-templates` / `demo-wide`, `dryRun=true` | `{"config": {"index_patterns": ["*"]}}` | 403: templates for every index are blocked |
| 3.24 | `PUT …/ilm-policies/demo-logs-policy` | `{"config": {"policy": {"phases": {"hot": {"actions": {"rollover": {"max_age": "3d"}}}}}}, "reason": "t"}` | `diff.changed` shows `7d` → `3d`; rollback restores `7d` |
| 3.25 | `PUT …/ingest-pipelines/demo-pipeline`, `dryRun=true` | `{"config": {"processors": [{"set": {"field": "env", "value": "test"}}]}, "sampleDocs": [{"msg": "hello"}]}` | `simulation.docs[0].doc._source` = `{"msg": "hello", "env": "test"}` |

### Permissions and audit

| # | As | Endpoint | Expect |
| --- | --- | --- | --- |
| 3.26 | **bob** | `GET …/cluster-settings` | 200 (bob has view) |
| 3.27 | **bob** | `PUT …/cluster-settings` with any change | 403 `PERMISSION_DENIED` |
| 3.28 | **bob** | `GET /api/v1/admin/users` | 403 `ADMIN_REQUIRED` |
| 3.29 | **admin** | `GET /api/v1/admin/audit` with today's **UTC** date, e.g. `2026-09-28` | every action above, with user, reason, diff and outcome, rejected attempts included |

To see what the API stored, open `local-test\.store` in File Explorer. It holds
`snapshots\`, `state\` and `audit\` as readable JSON, the same layout the API uses in S3.

## 4. Automated checks (optional)

With Elasticsearch running (step 1), from the `es-config-api` folder:

```cmd
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

This runs 57 tests against your local Elasticsearch, with S3 simulated. With the API
running (step 2), you can also run:

```cmd
.venv\Scripts\python scripts\smoke_test.py --admin latish.madapada@fenixcommerce.com --admin-password Local-Test-Admin-2026 --cluster local --es-url http://127.0.0.1:9200 --es-password changeme123
```

The smoke test replaces the allowlist, so repeat step 2.2 afterwards if you want to keep
testing by hand.

## 5. Stop and clean up

- Stop the API: press Ctrl+C in its window.
- Stop Elasticsearch: close its window (titled `elasticsearch`).
- Start fresh: delete `local-test\.store` (the API's state) and `%USERPROFILE%\es-local\data`
  (the Elasticsearch data; step 1 then recreates the users and demo data).
- Remove everything: delete `%USERPROFILE%\es-local` and `es-config-api\.venv`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Step 1 is stuck on "Downloading" | Corporate proxy or DLP software may block it. Download `elasticsearch-8.17.1-windows-x86_64.zip` in a browser and save it to `%USERPROFILE%\es-local\` |
| "Elasticsearch did not start" | Check the minimized Elasticsearch window or `%USERPROFILE%\es-local\logs`. Another program may be using port 9200 |
| `pip install failed` | Behind a proxy: `set HTTPS_PROXY=http://proxy:port` before step 2 |
| 401 `NOT_AUTHENTICATED` / `SESSION_EXPIRED` in /docs | Sign in again and paste the new token into **Authorize** |
| 423 `ACCOUNT_LOCKED` | Five wrong passwords: wait 15 minutes or have an admin reset the password |
| Every change returns 403 `NOT_ALLOWLISTED` | Set the allowlist (step 2.2). A new install blocks everything |
