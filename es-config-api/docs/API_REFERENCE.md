# ES Config API — Endpoint Reference

*Last updated 29 September 2026. Kept in sync with the shared Claude Doc version of this reference. For the web console see [USER_GUIDE.md](USER_GUIDE.md).*

## Basics

Every endpoint is under `http://172.0.58.49/api/v1`. Sign in first (see **Sign in** below) and send your session token as `Authorization: Bearer $TOKEN`; every example uses `$TOKEN`. The examples are complete commands for cluster `elkm2-prod` and work from the EC2 instance or any machine the port-80 security-group rule allows.

**Prefer a browser?** The web console at <http://172.0.58.49/ui/> does everything below with buttons, dry-run previews and one-click rollback; see the [ES Config Console — User Guide](https://claude.ai/code/artifact/7bee11f6-6144-44fb-a2f9-e541376d1a14). This reference is for scripts and automation.

**In the browser:** open <http://172.0.58.49/docs>, run `POST /api/v1/auth/login` (**Try it out → Execute**) with your email and password, copy the `token` from the response, click **Authorize** and paste it. Every endpoint below can then be tried with **Try it out → Execute**.

**On Windows Command Prompt:** the examples are written for Linux/macOS shells (for example the EC2 terminal). `cmd` does not understand single quotes, so change every `'` to `"`, and for JSON bodies put the body in a file and send it with `-d @body.json`:

```bat
set TOKEN=<paste the token from the login response>
curl -s -H "Authorization: Bearer %TOKEN%" http://172.0.58.49/api/v1/clusters

curl -s -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json" -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings?dryRun=true" -d @body.json
```

`body.json` holds the JSON exactly as shown in the examples, for example `{"config": {"indices.recovery.max_bytes_per_sec": "60mb"}}`.

**Who can call what**

| Access | Can do |
| --- | --- |
| `view` on a cluster | All GET endpoints for that cluster |
| `edit` on a cluster | Also update, dry run and rollback |
| `delete` on a cluster | Also delete indices (the index must also match the `index-delete` allowlist) |
| `admin` | Everything, on every cluster, plus the `/admin` endpoints |

**Request body for every update** (PUT):

```json
{
  "config": { "...": "what to change" },
  "reason": "why — required for a real change, goes into the audit log",
  "sampleDocs": [ ]
}
```

`sampleDocs` is only used by the ingest-pipeline dry run. Rollback (POST) takes just `{"reason": "..."}`.

**Query options and headers on update and rollback**

| Option | Effect |
| --- | --- |
| `?dryRun=true` | Validate and preview; nothing is changed or snapshotted |
| `?force=true` | Go ahead despite drift (rollback) or a red cluster; logged as forced |
| `If-Match: "<version>"` header | Refuse with 412 if the config changed since you read it |

**What an update, rollback or dry run returns**

```json
{
  "changeId": "b377a448…",
  "action": "UPDATE",
  "dryRun": false,
  "applied": true,
  "versionBefore": "39117691957017cd",
  "versionAfter": "4fac5263100d87de",
  "diff": { "added": [], "removed": [], "changed": [] },
  "createsResource": false,
  "deletesResource": false,
  "warnings": [],
  "permanent": false,
  "driftDetected": false,
  "clusterHealth": "green",
  "rollbackAvailable": true
}
```

A dry run also returns `simulation` (for index templates and ingest pipelines) and `valid`. A request that would change nothing returns `"noChange": true`.

## Sign in

Every endpoint except `/healthz`, `/auth/login` and `/auth/config` needs a session. The web console keeps it in an HttpOnly cookie; scripts and curl send the token. Passwords are stored only as scrypt hashes and never logged.

### POST /api/v1/auth/login

Signs in with email and password and returns a token valid for 12 hours. Keep it in `$TOKEN`:

```bash
TOKEN=$(curl -s -X POST http://172.0.58.49/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"username": "latish.madapada@fenixcommerce.com", "password": "<your password>"}' \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])')
```

```json
{"token": "esc1.eyJzdWIi…", "tokenType": "Bearer", "expiresAt": "2026-09-29T18:39:20Z", "user": {"username": "latish.madapada@fenixcommerce.com", "admin": true, "usingGeneratedPassword": true}}
```

A wrong email or password returns 401 `INVALID_CREDENTIALS`. Five wrong passwords in a row lock the account for 15 minutes (423 `ACCOUNT_LOCKED`); an admin password reset unlocks it.

### GET /api/v1/me

Who you are signed in as, whether you still use the generated password, and your access per cluster.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/me
```

```json
{"username": "latish.madapada@fenixcommerce.com", "admin": true, "authMode": "password", "usingGeneratedPassword": true, "lastLoginAt": "2026-09-29T06:39:20.390Z", "clusters": {"elkm2-prod": "delete"}}
```

### POST /api/v1/auth/change-password

Replaces your password (at least 12 characters, letters and digits). Your other sessions end; the response carries a new token to use from now on.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/auth/change-password \
  -d '{"currentPassword": "<password from the CSV>", "newPassword": "<your new password>"}'
```

### POST /api/v1/auth/logout and /api/v1/auth/logout-all

`logout` clears the browser cookie. `logout-all` ends every session you have, tokens included.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -X POST http://172.0.58.49/api/v1/auth/logout-all
```

### GET /api/v1/auth/config

Public (no token): the sign-in rules, used by the console's sign-in page.

```bash
curl -s http://172.0.58.49/api/v1/auth/config
```

```json
{"authMode": "password", "sessionHours": 12, "lockoutAttempts": 5, "lockoutMinutes": 15}
```

## Health and discovery

Four read-only endpoints: whether the API is up, which clusters you can use, and what is in them.

### GET /healthz

Checks that the API process is running. No sign-in needed; used by Docker's health check.

```bash
curl -s http://172.0.58.49/healthz
```

```json
{"status": "ok", "clusters": 1}
```

### GET /api/v1/clusters

Lists only the clusters you have access to, with your permission level on each.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters
```

```json
{"items": [{"id": "elkm2-prod", "name": "ELK M2 Production", "description": "", "tags": [], "permission": "edit"}]}
```

### GET /api/v1/clusters/{clusterId}/health

Returns the cluster's health. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/health
```

```json
{"clusterId": "elkm2-prod", "status": "green", "numberOfNodes": 3, "unassignedShards": 0, "clusterName": "alpha-elkm-cluster-1"}
```

### GET /api/v1/clusters/{clusterId}/indices

Lists the cluster's indices (dot/system indices are hidden), to find the name to use in the index endpoints. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/indices
```

```json
{"clusterId": "elkm2-prod", "items": [{"index": "delest-log-2026.09.26", "health": "green", "status": "open", "docs.count": "72523139"}, {"index": "my-index", "health": "green", "status": "open", "docs.count": "1"}]}
```

## Cluster settings

Persistent cluster-wide settings (`_cluster/settings`). Updates are partial: send only the keys you want to change; `null` resets a key to its Elasticsearch default. Keys must be on the allowlist.

### GET /api/v1/clusters/{clusterId}/cluster-settings

Returns the current persistent settings, their `version` (also in the `ETag` header), whether a rollback is available, and whether someone changed them outside the API (`driftDetected`). Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings
```

```json
{
  "clusterId": "elkm2-prod",
  "configType": "cluster-settings",
  "resource": "_cluster",
  "version": "39117691957017cd",
  "config": {},
  "rollbackAvailable": false,
  "driftDetected": false,
  "lastApplied": null,
  "warnings": []
}
```

### PUT /api/v1/clusters/{clusterId}/cluster-settings

Changes settings. The API snapshots the current settings to S3 first, applies the change, and records it in the audit log. Needs `edit`.

Preview first with `?dryRun=true` (no `reason` needed):

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings?dryRun=true" \
  -d '{"config": {"indices.recovery.max_bytes_per_sec": "60mb"}}'
```

```json
{"dryRun": true, "applied": false, "diff": {"added": [{"path": "indices.recovery.max_bytes_per_sec", "after": "60mb"}], "removed": [], "changed": []}, "clusterHealth": "green", "valid": true}
```

Then apply it. Adding `If-Match` with the version from the GET protects against someone else changing it in between:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'If-Match: "39117691957017cd"' -X PUT http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings \
  -d '{"config": {"indices.recovery.max_bytes_per_sec": "60mb"}, "reason": "Faster recovery during node replacement"}'
```

```json
{"applied": true, "versionBefore": "39117691957017cd", "versionAfter": "4fac5263100d87de", "rollbackAvailable": true, "snapshotKey": "snapshots/elkm2-prod/cluster-settings/_cluster.json"}
```

Reset a key to its default by sending `null`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings \
  -d '{"config": {"indices.recovery.max_bytes_per_sec": null}, "reason": "Back to default"}'
```

### POST /api/v1/clusters/{clusterId}/cluster-settings/rollback

Restores the settings stored in the last snapshot. Rollback swaps: what was live becomes the new snapshot, so rolling back twice re-applies the change. Refused with 409 if the settings were changed outside the API since (use `?force=true` if you are sure). Supports `?dryRun=true`. Needs `edit`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings/rollback \
  -d '{"reason": "Recovery finished"}'
```

```json
{"action": "ROLLBACK", "applied": true, "diff": {"added": [], "removed": [{"path": "indices.recovery.max_bytes_per_sec", "before": "60mb"}], "changed": []}}
```

### GET /api/v1/clusters/{clusterId}/cluster-settings/previous

Shows the stored snapshot a rollback would restore: the config, who captured it and when. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/cluster-settings/previous
```

```json
{"rollbackSupported": true, "state": {"exists": true, "config": {}}, "version": "39117691957017cd", "capturedAt": "2026-09-29T02:10:05.120Z", "capturedBy": "latish.madapada@fenixcommerce.com", "replacedBy": "UPDATE"}
```

## Index settings and mappings

One concrete index per request: no wildcards, commas or `_all`. `{part}` is `settings` or `mapping`.

### GET /api/v1/clusters/{clusterId}/indices/{index}/settings

Returns the index's settings in flat form, without read-only keys such as `index.uuid`. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/settings
```

```json
{"resource": "delest-log-2026.09.26", "version": "a1c9…", "config": {"index.number_of_replicas": "1", "index.number_of_shards": "3", "index.refresh_interval": "1s"}}
```

### PUT /api/v1/clusters/{clusterId}/indices/{index}/settings

Changes dynamic index settings; the `index.` prefix is optional. Static settings (shard count, analyzers, codec and so on) are refused with 422, because they need the index closed. Supports `?dryRun=true`. Needs `edit`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/settings \
  -d '{"config": {"refresh_interval": "30s"}, "reason": "Testing the API"}'
```

```json
{"applied": true, "diff": {"added": [], "removed": [], "changed": [{"path": "index.refresh_interval", "before": "1s", "after": "30s"}]}}
```

### POST /api/v1/clusters/{clusterId}/indices/{index}/settings/rollback

Restores the index settings from the last snapshot. Supports `?dryRun=true` and `?force=true`. Needs `edit`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/settings/rollback \
  -d '{"reason": "Test done"}'
```

### GET /api/v1/clusters/{clusterId}/indices/{index}/settings/previous

Shows the stored snapshot a rollback would restore. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/settings/previous
```

### GET /api/v1/clusters/{clusterId}/indices/{index}/mapping

Returns the index's field mapping. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/mapping
```

```json
{"resource": "delest-log-2026.09.26", "config": {"properties": {"@timestamp": {"type": "date"}, "message": {"type": "text"}}}}
```

### PUT /api/v1/clusters/{clusterId}/indices/{index}/mapping

Adds new fields. **It is add-only and permanent**: existing fields can't be changed or removed, and there is no rollback. Always dry-run first. Needs `edit`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/mapping?dryRun=true" \
  -d '{"config": {"properties": {"brand": {"type": "keyword"}}}}'
```

```json
{"dryRun": true, "permanent": true, "rollbackAvailableAfter": false, "warnings": ["Mapping changes are permanent: new fields cannot be removed and this change cannot be rolled back"]}
```

Changing an existing field is refused:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/delest-log-2026.09.26/mapping?dryRun=true" \
  -d '{"config": {"properties": {"@timestamp": {"type": "keyword"}}}}'
# 422 MAPPING_CONFLICT ("@timestamp" is already a date field)
```

`POST …/mapping/rollback` always returns 422 `ROLLBACK_NOT_SUPPORTED`.

### DELETE /api/v1/clusters/{clusterId}/indices/{index}

Deletes one index. **This is permanent: the documents cannot be recovered.** Before deleting, the API saves the index's settings, mappings and aliases to S3 so an empty copy can be recreated, but not the data. Needs the `delete` permission level and a matching `index-delete` allowlist pattern.

| Query parameter | Required | Meaning |
| --- | --- | --- |
| `dryRun=true` | — | Show what would be deleted; deletes nothing |
| `confirm` | yes, unless dry run | The exact index name again |
| `reason` | yes, unless dry run | Why; goes into the audit log |

Refused for aliases, data-stream names and wildcards (use the concrete index name), and for the current write index of a data stream (roll the data stream over first). Dot/system indices need an `index-delete` pattern that starts with `.`.

Always dry-run first. It shows the document count, size and aliases:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X DELETE "http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/orders-2024.01?dryRun=true"
```

```json
{"dryRun": true, "applied": false, "index": "orders-2024.01", "status": "open", "docsCount": 184220, "storeSizeBytes": 96412330, "aliases": [], "dataStream": null, "permanent": true, "confirmRequired": "orders-2024.01", "warnings": ["Deleting an index permanently destroys its documents. …", "184,220 documents will be deleted"]}
```

Then delete it:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X DELETE "http://172.0.58.49/api/v1/clusters/elkm2-prod/indices/orders-2024.01?confirm=orders-2024.01&reason=Past%20retention"
```

```json
{"applied": true, "index": "orders-2024.01", "tombstoneKey": "deleted-indices/elkm2-prod/orders-2024.01/2026-09-29T103015.120Z_9c1e….json"}
```

In a URL, write spaces in `reason` as `%20`, or use `--data-urlencode` with `curl -G`.

### GET /api/v1/clusters/{clusterId}/deleted-indices

Lists indices deleted through the API, newest first: who deleted them, when, why, how many documents they held, and the saved settings and mappings. Filter with `?index=<name>`. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "http://172.0.58.49/api/v1/clusters/elkm2-prod/deleted-indices?index=orders-2024.01"
```

```json
{"clusterId": "elkm2-prod", "items": [{"index": "orders-2024.01", "deletedAt": "2026-09-29T10:30:15.120Z", "deletedBy": "latish.madapada@fenixcommerce.com", "reason": "Past retention", "docsCount": 184220, "definition": {"settings": {}, "mappings": {}, "aliases": {}}}]}
```

To recreate an empty index from it, take `definition.mappings` and `definition.settings.index.number_of_shards` / `number_of_replicas` and `PUT` them to Elasticsearch.

## Templates, ILM policies and ingest pipelines

These four types share the same five endpoints; only `{type}` and the body change. An update is a **full replace**: send the whole resource body, exactly as Elasticsearch expects it. If the resource doesn't exist yet it is created, and rolling back deletes it again.

| `{type}` | What it is | `config` body |
| --- | --- | --- |
| `index-templates` | Settings/mappings applied to new indices matching a pattern | `{"index_patterns": [...], "priority": n, "template": {...}, "composed_of": [...]}` |
| `component-templates` | Reusable building blocks for index templates | `{"template": {...}}` |
| `ilm-policies` | Index lifecycle (rollover, delete after N days) | `{"policy": {"phases": {...}}}` |
| `ingest-pipelines` | Processing steps run on documents at index time | `{"description": "...", "processors": [...]}` |

### GET /api/v1/clusters/{clusterId}/{type}

Lists the names of all resources of that type. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/ilm-policies
```

```json
{"clusterId": "elkm2-prod", "configType": "ilm-policies", "items": ["logs", "logs-30d", "metrics"]}
```

### GET /api/v1/clusters/{clusterId}/{type}/{name}

Returns one resource's body and `version`. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/ilm-policies/logs-30d
```

```json
{"resource": "logs-30d", "version": "7d2e…", "config": {"policy": {"phases": {"hot": {"min_age": "0ms", "actions": {"rollover": {"max_age": "7d"}}}, "delete": {"min_age": "30d", "actions": {"delete": {}}}}}}}
```

### PUT /api/v1/clusters/{clusterId}/{type}/{name}

Creates or replaces the resource, after snapshotting the current version. Supports `?dryRun=true`. Needs `edit`.

ILM policy, changing retention from 30 to 14 days:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/clusters/elkm2-prod/ilm-policies/logs-30d -d '{
  "config": {"policy": {"phases": {
    "hot": {"actions": {"rollover": {"max_age": "7d"}}},
    "delete": {"min_age": "14d", "actions": {"delete": {}}}}}},
  "reason": "Reduce log retention to 14 days"}'
```

Index template dry run. The response includes Elasticsearch's own simulation of the resolved template:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/index-templates/orders?dryRun=true" -d '{
  "config": {"index_patterns": ["orders-*"], "priority": 200,
             "template": {"settings": {"number_of_replicas": 1}}}}'
```

```json
{"dryRun": true, "createsResource": true, "simulation": {"kind": "index_template_simulate", "resolvedTemplate": {"settings": {"index": {"number_of_replicas": "1"}}}, "overlapping": []}}
```

Ingest pipeline dry run with sample documents. `simulation.docs` shows each document after the pipeline ran:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/ingest-pipelines/add-env?dryRun=true" -d '{
  "config": {"processors": [{"set": {"field": "env", "value": "prod"}}]},
  "sampleDocs": [{"msg": "hello"}]}'
```

```json
{"dryRun": true, "simulation": {"kind": "ingest_pipeline_simulate", "docs": [{"doc": {"_source": {"msg": "hello", "env": "prod"}}}]}}
```

Component template:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/clusters/elkm2-prod/component-templates/orders-settings \
  -d '{"config": {"template": {"settings": {"number_of_replicas": 1}}}, "reason": "Shared replica setting"}'
```

### POST /api/v1/clusters/{clusterId}/{type}/{name}/rollback

Puts back the previous version, or deletes the resource if it didn't exist before the last change (`"deletesResource": true`). Supports `?dryRun=true` and `?force=true`. Needs `edit`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/ilm-policies/logs-30d/rollback \
  -d '{"reason": "Keep 30 days after all"}'
```

### GET /api/v1/clusters/{clusterId}/{type}/{name}/previous

Shows the stored snapshot a rollback would restore. `state.exists: false` means a rollback would delete the resource. Needs `view`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/clusters/elkm2-prod/ilm-policies/logs-30d/previous
```

## Admin API

Admins only (users with `admin: true`, or listed in `BOOTSTRAP_ADMINS`). Every change made here is written to the audit log.

### GET /api/v1/admin/clusters

Lists every cluster in `clusters.yaml` and pings each one. Add `?check=false` to skip the ping.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/admin/clusters
```

```json
{"items": [{"id": "elkm2-prod", "name": "ELK M2 Production", "hosts": ["http://node4.elkm2.prod.int.fenixcommerce.com:9200"], "authType": "basic", "reachable": true, "version": "8.17.1", "clusterName": "alpha-elkm-cluster-1"}]}
```

### GET /api/v1/admin/users

Lists every user with their admin flag and per-cluster access.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/admin/users
```

```json
{"items": [{"username": "latish.madapada@fenixcommerce.com", "admin": true, "bootstrap": true, "clusters": {}, "hasPassword": true, "usingGeneratedPassword": false, "lastLoginAt": "2026-09-29T06:39:20.390Z"}, {"username": "priya@fenixcommerce.com", "admin": false, "bootstrap": false, "clusters": {"elkm2-prod": "edit"}, "hasPassword": true, "usingGeneratedPassword": true}]}
```

`usingGeneratedPassword: true` means the user has not changed the password from the CSV yet. Password hashes are never returned.

### GET /api/v1/admin/users/{username}

Returns one user.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/admin/users/priya@fenixcommerce.com
```

### POST /api/v1/admin/users

Adds a user by email. The API generates a 16-character password and returns it **once**; it is stored only as a hash. Add `?format=csv` to download it as `username,password`. The user can sign in straight away and change it later.

`clusters` maps a cluster id, or `"*"` for all clusters, to `view`, `edit` or `delete`; each level includes the ones below it. `"admin": true` gives full access.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST "http://172.0.58.49/api/v1/admin/users?format=csv" -o priya-credentials.csv \
  -d '{"username": "priya@fenixcommerce.com", "clusters": {"elkm2-prod": "edit"}}'
```

Without `?format=csv` the password comes back as JSON:

```json
{"user": {"username": "priya@fenixcommerce.com", "admin": false, "clusters": {"elkm2-prod": "edit"}, "hasPassword": true, "usingGeneratedPassword": true}, "credentials": {"username": "priya@fenixcommerce.com", "password": "<16 generated characters>"}, "note": "The password is shown only now. Download the CSV and hand it over securely."}
```

### POST /api/v1/admin/users/bulk

Adds several users at once, with one CSV for all. All or nothing: if any email already exists, nobody is created.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST "http://172.0.58.49/api/v1/admin/users/bulk?format=csv" -o new-users.csv \
  -d '{"users": [{"username": "sam@fenixcommerce.com", "clusters": {"*": "view"}}, {"username": "ravi@fenixcommerce.com", "admin": true}]}'
```

### POST /api/v1/admin/users/{email}/reset-password

For a forgotten password or a locked account: generates a new password (returned once), and the old password and all of the user's sessions stop working.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -X POST "http://172.0.58.49/api/v1/admin/users/priya@fenixcommerce.com/reset-password?format=csv" -o priya-new-password.csv
```

If the only admin is locked out, run this on the EC2 instance instead: `docker compose exec es-config-api python -m app.cli reset-password <email>`.

### PUT /api/v1/admin/users/{email}

Changes an existing user's admin flag and cluster access. The password is not touched.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/users/priya@fenixcommerce.com \
  -d '{"admin": false, "clusters": {"elkm2-prod": "delete"}}'
```

### PUT /api/v1/admin/users/{username}/permissions

Changes only the user's cluster access and keeps the admin flag. The body is the full new access map.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/users/priya@fenixcommerce.com/permissions -d '{"elkm2-prod": "view"}'
```

### DELETE /api/v1/admin/users/{username}

Removes a user. Bootstrap admins can't be deleted this way; remove them from `BOOTSTRAP_ADMINS` in `.env` instead.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X DELETE http://172.0.58.49/api/v1/admin/users/sam@fenixcommerce.com
```

### GET /api/v1/admin/allowlist

Shows the global allowlist: which settings and resources may be changed.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/admin/allowlist
```

### PUT /api/v1/admin/allowlist

Replaces the global allowlist. Patterns are globs; `deny` wins over `allow`, and a type that isn't listed is fully locked.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/allowlist -d '{
  "cluster-settings":    {"allow": ["indices.recovery.*", "cluster.routing.allocation.disk.*"],
                          "deny":  ["cluster.routing.allocation.enable"]},
  "index-settings":      {"allow": ["index.refresh_interval", "index.number_of_replicas"],
                          "indices": ["orders-*", "logs-*"]},
  "index-mappings":      {"allow": ["orders-*"]},
  "index-templates":     {"allow": ["orders*"], "indexPatterns": ["orders-*"]},
  "component-templates": {"allow": ["orders-*"]},
  "ilm-policies":        {"allow": ["*"]},
  "ingest-pipelines":    {"allow": ["*"]},
  "index-delete":        {"allow": ["orders-2024.*", "logs-*"]}
}'
```

| Type | `allow` / `deny` match | Extra key |
| --- | --- | --- |
| `cluster-settings` | setting keys | — |
| `index-settings` | setting keys | `indices`: which indices (default all non-system) |
| `index-mappings` | index names | — |
| `index-delete` | index names that may be deleted | — |
| `index-templates` | template names | `indexPatterns`: which index patterns a template may target |
| `component-templates`, `ilm-policies`, `ingest-pipelines` | resource names | — |

Dot/system indices only match patterns that themselves start with `.`.

### GET /api/v1/admin/allowlist/{clusterId}

Shows the allowlist that applies to one cluster, and whether it comes from the global list or a cluster override.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' http://172.0.58.49/api/v1/admin/allowlist/elkm2-prod
```

```json
{"clusterId": "elkm2-prod", "effectiveSource": "global", "rules": {"cluster-settings": {"allow": ["indices.recovery.*"], "deny": []}}}
```

### PUT /api/v1/admin/allowlist/{clusterId}

Sets an override for one cluster. It **replaces** the global list for that cluster; it does not add to it.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/allowlist/elkm2-prod -d '{
  "cluster-settings": {"allow": ["indices.recovery.max_bytes_per_sec"]}}'
```

### DELETE /api/v1/admin/allowlist/{clusterId}

Removes the override, so the global allowlist applies to that cluster again.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X DELETE http://172.0.58.49/api/v1/admin/allowlist/elkm2-prod
```

### GET /api/v1/admin/audit

Returns the audit events for one UTC day, newest first. Filter by `clusterId`, `user` or `action` (`UPDATE`, `ROLLBACK`, `DRY_RUN`, `ADMIN_USER_UPDATE`, `ADMIN_PERMISSIONS_UPDATE`, `ADMIN_USER_DELETE`, `ADMIN_ALLOWLIST_UPDATE`, `ADMIN_ALLOWLIST_DELETE`, `ADMIN_USER_CREATE`, `ADMIN_PASSWORD_RESET`, `INDEX_DELETE`, `AUTH_LOGIN`, `AUTH_LOGOUT`, `AUTH_LOGOUT_ALL`, `AUTH_PASSWORD_CHANGE`); `limit` defaults to 200.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "http://172.0.58.49/api/v1/admin/audit?date=2026-09-29&clusterId=elkm2-prod&action=UPDATE"
```

```json
{"date": "2026-09-29", "count": 1, "items": [{
  "timestamp": "2026-09-29T02:10:05.412Z", "actor": "latish.madapada@fenixcommerce.com", "sourceIp": "10.0.4.21",
  "action": "UPDATE", "clusterId": "elkm2-prod", "configType": "cluster-settings", "resource": "_cluster",
  "reason": "Faster recovery during node replacement", "outcome": "SUCCESS",
  "versionBefore": "39117691957017cd", "versionAfter": "4fac5263100d87de",
  "diff": {"added": [{"path": "indices.recovery.max_bytes_per_sec", "after": "60mb"}], "removed": [], "changed": []}}]}
```

`outcome` is `SUCCESS`, `NO_CHANGE`, `REJECTED` (a check refused it) or `FAILED` (Elasticsearch or the cluster failed).

## Error codes

Every error has the same shape: an HTTP status, a stable `code`, a readable `message`, and sometimes `details`. A rejected request never changes anything.

```json
{"error": {"code": "NOT_ALLOWLISTED", "message": "Not on the global allowlist for cluster-settings", "details": {"blocked": ["cluster.routing.allocation.enable"], "allowlist": "global"}}}
```

| Status | Code | Meaning / fix |
| --- | --- | --- |
| 400 | `INVALID_REQUEST`, `INVALID_CONFIG` | Body is malformed or missing a required part (for example an index template without `index_patterns`) |
| 400 | `REASON_REQUIRED` | Add `"reason"` to a real (non dry-run) change |
| 400 | `CONFIRMATION_MISMATCH` | Index delete: `confirm` must be the exact index name |
| 400 | `ES_REJECTED` | Elasticsearch refused the config (unknown setting, bad value); `message` has its reason |
| 400 | `INVALID_INDEX_NAME`, `INVALID_RESOURCE_NAME` | Use one concrete name: no `*`, commas or `_all` |
| 400 | `INVALID_ALLOWLIST`, `INVALID_PERMISSIONS`, `UNKNOWN_CLUSTER` | Admin body is wrong: patterns must be lists, levels `view`/`edit`/`delete`, cluster ids from `clusters.yaml` |
| 400 | `INVALID_EMAIL` | New users are added by email address |
| 400 | `WEAK_PASSWORD` | New password needs 12+ characters with letters and digits (or 20+ of anything), must not be your email and must differ from the current one |
| 400 | `CANNOT_DELETE_SELF` | An admin can't delete their own user |
| 401 | `NOT_AUTHENTICATED` | No token sent: sign in with `POST /api/v1/auth/login` and send `Authorization: Bearer $TOKEN` (in /docs: **Authorize**) |
| 401 | `SESSION_EXPIRED` | Token expired (after 12 hours) or ended by a password change, reset or logout-all; sign in again |
| 401 | `INVALID_CREDENTIALS` | Wrong email or password (sign-in or change-password) |
| 403 | `CSRF_CHECK_FAILED` | Browser-cookie write without the `X-Requested-With` header; scripts should use the Bearer token instead |
| 403 | `PERMISSION_DENIED` | Your level on that cluster is too low (`edit` to change, `delete` to delete indices) |
| 403 | `NOT_ALLOWLISTED` | A key, resource or index isn't on the allowlist; `details.blocked` lists which |
| 403 | `ADMIN_REQUIRED` | Admin-only endpoint |
| 404 | `CLUSTER_NOT_FOUND`, `INDEX_NOT_FOUND`, `RESOURCE_NOT_FOUND`, `USER_NOT_FOUND` | Check the id or name |
| 404 | `NO_SNAPSHOT` | Nothing to roll back yet: no change has been made through the API |
| 409 | `USER_EXISTS` | That email already has an account; use reset-password or `PUT /admin/users/{email}` |
| 409 | `CHANGE_IN_PROGRESS` | Someone else is changing the same resource; retry in a moment |
| 409 | `DRIFT_DETECTED` | Config was changed outside the API; check it, then roll back with `?force=true` if still wanted |
| 409 | `CLUSTER_UNHEALTHY` | Cluster is red; fix it first, or use `?force=true` |
| 409 | `CONCURRENT_CHANGE`, `LOCK_LOST` | A parallel change collided or took too long; GET the resource and retry |
| 412 | `VERSION_MISMATCH` | The config changed since your GET (`If-Match`); read it again |
| 422 | `STATIC_SETTING`, `READ_ONLY_SETTING` | This index setting can't be changed on an open index |
| 422 | `MAPPING_CONFLICT`, `MAPPING_NOT_ADD_ONLY` | Mappings can only gain new fields |
| 422 | `ROLLBACK_NOT_SUPPORTED` | Mapping changes are permanent |
| 422 | `NOT_A_CONCRETE_INDEX` | The name is an alias, a data stream, or matches several indices; use the real index name |
| 422 | `DATA_STREAM_WRITE_INDEX` | Can't delete a data stream's current write index; roll it over first |
| 423 | `ACCOUNT_LOCKED` | 5 wrong passwords in a row: wait 15 minutes (`details.retryAfterMinutes`) or ask an admin to reset the password |
| 502 | `CLUSTER_UNREACHABLE` | The API can't reach Elasticsearch (network or security group) |
| 502 | `ES_AUTH_FAILED` | Wrong Elasticsearch username or password in `.env` |
| 502 | `ES_UNAVAILABLE` | Elasticsearch returned a 5xx or 429; retry, and check the cluster |

Every response carries an `X-Request-ID` header; quote it when reporting a problem, since the same id is in the audit log.
