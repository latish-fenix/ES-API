# ES Config API — Endpoint Reference

*Last updated 30 September 2026: roll back any change (config history, recreate deleted indices, recent document changes), index-level permissions, cluster settings for admins only, node stats, document edits and bulk changes with undo, every secret in AWS Secrets Manager. Kept in sync with the shared Claude Doc version of this reference. For the web console see [USER_GUIDE.md](USER_GUIDE.md).*

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

Each user has, per cluster, a **default level** and optional **index rules** (an index pattern with its own level, `none` to hide matching indices; the most specific matching pattern wins). For index settings, mappings, index delete and documents the user's level **on that index** counts; for templates, ILM policies and pipelines the cluster default counts.

| Access | Can do |
| --- | --- |
| `view` | Read config; search, read and export documents |
| `edit` | Also update, dry run and roll back; create, edit and delete single documents; bulk update |
| `delete` | Also delete indices (the index must also match the `index-delete` allowlist); bulk delete documents |
| `admin` | Everything, on every cluster, including **cluster settings** (admins only) and the `/admin` endpoints |

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
{"username": "priya@fenixcommerce.com", "admin": false, "authMode": "password", "usingGeneratedPassword": false,
 "lastLoginAt": "2026-09-30T06:39:20.390Z",
 "clusters": {"elkm2-prod": "view"},
 "access": {"elkm2-prod": {"default": "view", "indices": [{"pattern": "shoppremium*", "level": "edit"}]}}}
```

`clusters` is the cluster-wide level; `access` adds the index rules for every cluster the user can open.

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

### GET /api/v1/clusters/{clusterId}/nodes

Every node with its CPU, RAM, JVM heap, disk and shard count, a cluster summary, and the disk watermarks. Any access on the cluster is enough. The console's Overview refreshes it every 30 seconds.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/clusters/elkm2-prod/nodes
```

```json
{"clusterId": "elkm2-prod",
 "nodes": [{"id": "0IMKiA7lTRmaVDb5qK2ZpA", "name": "node-1", "ip": "10.0.4.31", "version": "8.17.1",
   "roles": ["data", "ingest", "master", "ml"], "master": true, "data": true,
   "cpuPercent": 12, "load1m": 0.12, "cpus": 2,
   "memTotalBytes": 6273757184, "memUsedBytes": 2333827072, "memUsedPercent": 37,
   "heapUsedBytes": 505386352, "heapMaxBytes": 1073741824, "heapUsedPercent": 47,
   "diskTotalBytes": 270553174016, "diskAvailableBytes": 28409073664, "diskUsedPercent": 89.5,
   "shards": 10, "uptimeMillis": 2101268}],
 "summary": {"nodes": 1, "dataNodes": 1, "diskTotalBytes": 270553174016, "diskAvailableBytes": 28409073664,
   "diskUsedPercent": 89.5, "cpuPercentAvg": 12.0, "cpuPercentMax": 12, "memUsedPercentAvg": 37.0,
   "heapUsedPercentAvg": 47.0, "heapUsedPercentMax": 47},
 "watermarks": {"low": 85.0, "high": 90.0, "flood": 95.0}}
```

`diskAvailableBytes` is what Elasticsearch can still use (it compares that with the watermarks). `memUsedPercent` is the operating system's view and includes the file-system cache on Linux, so a high value is normal; watch `heapUsedPercent` and disk instead.

## Cluster settings

**Admins only** (every endpoint in this section returns `403 ADMIN_REQUIRED` for other users). Persistent cluster-wide settings (`_cluster/settings`). Updates are partial: send only the keys you want to change; `null` resets a key to its Elasticsearch default. Keys must be on the allowlist.

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

## Data

Search and read the documents in an index (`view` on it), and change them (`edit` / `delete`, see *Changing documents* below). A search on a pattern only returns documents from the indices the user may view; `hiddenIndices` in the response says how many matching indices were left out. Only non-system indices: a name or pattern starting with `.` is refused (`SYSTEM_INDEX`), and wildcards never reach dot or hidden indices. Every search, document read and export is written to the audit log (`DATA_SEARCH`, `DATA_DOCUMENT`, `DATA_EXPORT`) with the query and the hit count, never the documents. Responses carry `Cache-Control: no-store`.

`{index}` in the paths below is one index name, alias or wildcard pattern, e.g. `shoppremiumoutlets.myshopify.com-shipment_summary-2024.09` or `shoppremiumoutlets*-2024.*` (no commas). The API's Elasticsearch account needs the `read` index privilege (see `docs/es-lockdown.md`); without it searches fail with `ES_READ_NOT_ALLOWED`.

**Search body** (`_search` and `_export`)

| Field | Default | Notes |
| --- | --- | --- |
| `query` | `""` (everything) | Lucene query string: `field:value`, `AND` / `OR` / `NOT`, wildcards `*`, ranges `order_info.total_price:[100 TO 200]`. Words separated by spaces must all match |
| `filters` | `[]` | List of `{"field", "op", …}`, all must match. `op`: `is` / `is_not` (+ `value`), `one_of` / `not_one_of` (+ `values`), `contains` (+ `value`, case-insensitive), `between` (+ `gte` and/or `lte`), `exists`, `not_exists`. Fields inside `nested` objects work too |
| `timeRange` | none | `{"field": "created_date", "gte": "now-7d", "lte": "…"}`: ISO date/time or date math |
| `sort` | by relevance | `[{"field": "created_date", "order": "desc"}]`; keyword, number and date fields only (text fields can't be sorted) |
| `from`, `size` | `0`, `25` | `size` up to 100; `from + size` up to 10,000 |

### GET /api/v1/clusters/{clusterId}/data/{index}/_fields

The fields of an index or pattern, with their type and whether they can be sorted (`aggregatable`). Also lists the matching indices, nested paths and date fields (for a time range).

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_fields
```

```json
{"clusterId": "elkm2-prod", "index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09",
 "indices": ["shoppremiumoutlets.myshopify.com-shipment_summary-2024.09"],
 "fields": [
   {"name": "created_date", "type": "date", "types": null, "searchable": true, "aggregatable": true, "object": false},
   {"name": "order_info", "type": "object", "types": null, "searchable": false, "aggregatable": false, "object": true},
   {"name": "order_info.order_number", "type": "keyword", "types": null, "searchable": true, "aggregatable": true, "object": false},
   {"name": "vendor", "type": "keyword", "types": null, "searchable": true, "aggregatable": true, "object": false}],
 "nestedPaths": ["line_items"], "dateFields": ["created_date", "estimated_delivery"]}
```

`type` is `conflict` (with `types` listed) when the indices of a pattern map a field differently.

### POST /api/v1/clusters/{clusterId}/data/{index}/_search

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_search -d '{
  "query": "vendor:2593 AND carrier:UPS",
  "filters": [{"field": "shipping_address.city", "op": "one_of", "values": ["Austin", "Denver"]}],
  "timeRange": {"field": "created_date", "gte": "2024-09-10T00:00:00Z", "lte": "2024-09-20T23:59:59Z"},
  "sort": [{"field": "created_date", "order": "desc"}],
  "from": 0, "size": 25
}'
```

```json
{"clusterId": "elkm2-prod", "index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09",
 "total": 146, "totalRelation": "eq", "took": 9, "timedOut": false, "from": 0, "size": 25, "maxWindow": 10000,
 "shards": {"total": 1, "failed": 0},
 "hits": [{"_index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "_id": "5138627053834", "_score": null,
   "_source": {"vendor": "2593", "created_date": "2024-09-20T23:11:00Z", "status": "exception", "carrier": "UPS",
     "order_info": {"order_number": "SP0286641658", "order_id": "5487441563574", "total_price": 177.36, "currency": "USD"},
     "shipping_address": {"city": "Austin", "province": "TX", "country": "US"}},
   "sort": [1726873860000]}]}
```

Page with `from` (0, 25, 50…). Past 10,000:

```json
{"error": {"code": "RESULT_WINDOW_EXCEEDED", "message": "Only the first 10,000 results can be paged through; narrow the search or change the sort"}}
```

A filter on a nested field, and a partial match:

```json
{"filters": [{"field": "line_items.sku", "op": "is", "value": "SKU-1731"},
             {"field": "order_info.order_number", "op": "contains", "value": "sp028664"}]}
```

### GET /api/v1/clusters/{clusterId}/data/{index}/_doc/{id}

One document, by its concrete `_index` (from a search hit) and `_id`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_doc/5138570242928
```

```json
{"_index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "_id": "5138570242928", "_version": 1, "_seq_no": 2060, "_primary_term": 1,
 "_source": {"vendor": "2593", "created_date": "2024-09-20T07:56:00Z", "status": "out_for_delivery", "carrier": "UPS", "order_info": {"order_number": "SP0286376220", "…": "…"}}}
```

### POST /api/v1/clusters/{clusterId}/data/{index}/_export

Downloads the matching documents (same body as `_search`, without `from`/`size`) as `csv`, `json` (an array) or `ndjson` (one per line). `limit` defaults to and is capped at 10,000. For CSV, `columns` picks dotted field paths; leave it empty for every field found. Values inside arrays of objects are joined as a JSON list; text starting with `=`, `+`, `-` or `@` is prefixed with `'` so spreadsheets don't run it as a formula.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_export -d '{
  "query": "vendor:2593",
  "sort": [{"field": "created_date", "order": "desc"}],
  "format": "csv",
  "columns": ["vendor", "created_date", "order_info.order_number", "status"],
  "limit": 3
}' -o vendor-2593.csv
```

```text
_index,_id,vendor,created_date,order_info.order_number,status
shoppremiumoutlets.myshopify.com-shipment_summary-2024.09,5138651087999,2593,2024-09-30T23:46:00Z,SP0286753953,out_for_delivery
shoppremiumoutlets.myshopify.com-shipment_summary-2024.09,5138598703814,2593,2024-09-30T23:22:00Z,SP0286509198,out_for_delivery
shoppremiumoutlets.myshopify.com-shipment_summary-2024.09,5138557857612,2593,2024-09-30T22:59:00Z,SP0286318352,exception
```

The file name is in `Content-Disposition` and the row count in `X-Export-Rows`. The CSV starts with a UTF-8 byte-order mark so Excel reads it correctly.

A system index is refused before Elasticsearch is asked:

```json
{"error": {"code": "SYSTEM_INDEX", "message": "System and hidden indices (names starting with '.') can't be browsed"}}
```

### Changing documents

With `edit` on an index you can create, replace and delete its documents; with `delete` you can also bulk-delete. Every change:

- needs a `reason` (except dry runs) and is audited as `DATA_DOC_CREATE` / `DATA_DOC_UPDATE` / `DATA_DOC_DELETE` / `DATA_DOC_RESTORE` / `DATA_BULK_UPDATE` / `DATA_BULK_DELETE` / `DATA_BULK_RESTORE`, with the field names that changed, never values;
- saves the document's previous state to S3 first (`doc-versions/…` for one document, `bulk-backups/…` for bulk), so it can be put back;
- works on a concrete index (not a pattern or alias). Documents in data streams are read-only here; system indices are refused.

The API's Elasticsearch account needs the `write` index privilege; without it writes fail with `ES_WRITE_NOT_ALLOWED`.

### PUT /api/v1/clusters/{clusterId}/data/{index}/_doc/{id}

Replaces a document with `document` (the whole `_source`). Send the `_seq_no` / `_primary_term` you read as `ifSeqNo` / `ifPrimaryTerm`: if someone changed the document since, it is refused (`DOCUMENT_CHANGED`) instead of overwritten. `?dryRun=true` returns the field-level diff and changes nothing.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT "http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_doc/5138553937707?dryRun=true" -d '{
  "document": {"vendor": "2593", "status": "delivered", "carrier": "UPS", "order_info": {"order_number": "SP0286300037"}},
  "ifSeqNo": 12501, "ifPrimaryTerm": 2
}'
```

```json
{"index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "id": "5138553937707", "dryRun": true, "noChange": false,
 "diff": {"added": [], "removed": [], "changed": [{"path": "status", "before": "exception", "after": "delivered"}]},
 "seqNo": 12501, "primaryTerm": 2}
```

Run it again without `dryRun` and with `"reason": "Carrier confirmed delivery"`:

```json
{"index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "id": "5138553937707", "applied": true,
 "changeId": "a09c56f61518406e8f88ef471818fef1", "seqNo": 14069, "primaryTerm": 2,
 "versionKey": "doc-versions/elkm2-prod/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/5138553937707/20260930T054244308099_a09c56f61518406e8f88ef471818fef1.json",
 "diff": {"added": [], "removed": [], "changed": [{"path": "status", "before": "exception", "after": "delivered"}]}}
```

Sending the old `ifSeqNo` again:

```json
{"error": {"code": "DOCUMENT_CHANGED", "message": "Document '5138553937707' changed since you read it. Reload it and try again"}}
```

### POST /api/v1/clusters/{clusterId}/data/{index}/_doc

Creates a document. `id` is optional (Elasticsearch picks one if left out); an existing id is refused (`DOCUMENT_EXISTS`). Returns 201.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_doc -d '{
  "id": "manual-001",
  "document": {"vendor": "2593", "status": "label_created", "order_info": {"order_number": "SP0286999001"}},
  "reason": "Recreate order lost in import"
}'
```

```json
{"index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "id": "manual-001", "applied": true,
 "changeId": "ec66d7c983724dfb8116f92f176444c9", "seqNo": 14070, "primaryTerm": 2, "versionKey": "doc-versions/elkm2-prod/…/manual-001/…json"}
```

### DELETE /api/v1/clusters/{clusterId}/data/{index}/_doc/{id}

Deletes a document. `confirm` must repeat the id and `reason` is required; `?dryRun=true` shows what would go. A copy is saved first.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -X DELETE "http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_doc/manual-001?confirm=manual-001&reason=Test%20order"
```

```json
{"index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "id": "manual-001", "applied": true, "deleted": true,
 "changeId": "d3e31fdbf4044fdf86b1d54296ecd19a", "versionKey": "doc-versions/elkm2-prod/…/manual-001/…json"}
```

### GET /api/v1/clusters/{clusterId}/data/{index}/_doc/{id}/_history

The saved versions of a document, newest first: what it looked like **before** each edit, create, delete or restore made through the API.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_doc/5138553937707/_history
```

```json
{"index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "id": "5138553937707", "items": [
  {"key": "doc-versions/elkm2-prod/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/5138553937707/20260930T054244308099_a09c56f6….json",
   "action": "UPDATE", "at": "2026-09-30T05:42:44.308Z", "by": "latish.madapada@fenixcommerce.com",
   "reason": "Carrier confirmed delivery", "changedFields": ["status"], "changeId": "a09c56f6…",
   "before": {"exists": true, "seqNo": 12501, "primaryTerm": 2, "source": {"vendor": "2593", "status": "exception", "…": "…"}}}]}
```

### POST /api/v1/clusters/{clusterId}/data/{index}/_doc/{id}/_restore

Puts a document back to a saved version (`versionKey` from `_history`): overwrites it, recreates it if it was deleted, or deletes it if the version is "before it was created". The current state is saved first, so a restore can be undone too. `?dryRun=true` returns the `plan` and the diff.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_doc/5138553937707/_restore -d '{
  "versionKey": "doc-versions/elkm2-prod/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/5138553937707/20260930T054244308099_a09c56f61518406e8f88ef471818fef1.json",
  "reason": "Undo: delivery was not confirmed"
}'
```

```json
{"index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "id": "5138553937707", "plan": "overwrite", "applied": true,
 "diff": {"added": [], "removed": [], "changed": [{"path": "status", "before": "delivered", "after": "exception"}]},
 "restoresTo": {"at": "2026-09-30T05:42:44.308Z", "by": "latish.madapada@fenixcommerce.com", "action": "UPDATE"}}
```

### POST /api/v1/clusters/{clusterId}/data/{index}/_bulk_update and …/_bulk_delete

Changes or deletes **every document the search matches** (same `query`, `filters`, `timeRange` as `_search`), at most 10,000. `_bulk_update` takes `set` (dotted field → new value) and/or `remove` (list of dotted fields). `_bulk_update` needs `edit`, `_bulk_delete` needs `delete`, on every index involved.

The dry run is mandatory:

1. `?dryRun=true` returns the exact `count`, how many would change, up to 5 samples with their before/after, warnings (for example "no query: matches every document") and a `dryRunToken` valid for 15 minutes.
2. The real call sends the **same** body plus `dryRunToken`, `expectedCount` (the count from step 1) and `reason`. It is refused if the body differs (`DRY_RUN_MISMATCH`), the token is missing or old (`DRY_RUN_REQUIRED`, `DRY_RUN_EXPIRED`), the count is wrong (`COUNT_MISMATCH`), or the matching documents changed in the meantime (`COUNT_CHANGED`).
3. All matching documents are backed up to S3, then written one by one with their `_seq_no` check: a document edited by someone else after the dry run is left alone and counted in `conflicts`.

```bash
# 1) dry run
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST "http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_bulk_update?dryRun=true" -d '{
  "query": "vendor:2121 AND carrier:USPS",
  "filters": [{"field": "status", "op": "is", "value": "exception"}],
  "set": {"status": "on_hold"}
}'
```

```json
{"op": "update", "index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "dryRun": true,
 "count": 76, "willChange": 76, "unchanged": 0, "skippedCount": 0, "skipped": [], "warnings": [],
 "indices": ["shoppremiumoutlets.myshopify.com-shipment_summary-2024.09"],
 "sample": [{"_index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "_id": "5138593619816",
   "diff": {"added": [], "removed": [], "changed": [{"path": "status", "before": "exception", "after": "on_hold"}]}}],
 "dryRunToken": "eyJjaGFuZ2UiOjc2LCJjb3VudCI6NzYs….rYhqn1ostdjmIggltXEjxeD0PZ0dOR8MvMRnqcVIp-I", "expiresInSeconds": 900,
 "confirm": "Send the same request with dryRunToken, expectedCount=76 and a reason"}
```

```bash
# 2) the real change: same body + token, count and reason
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_bulk_update -d '{
  "query": "vendor:2121 AND carrier:USPS",
  "filters": [{"field": "status", "op": "is", "value": "exception"}],
  "set": {"status": "on_hold"},
  "dryRunToken": "<dryRunToken from step 1>", "expectedCount": 76,
  "reason": "Hold USPS exceptions for vendor 2121"
}'
```

```json
{"op": "update", "count": 76, "willChange": 76, "applied": true, "changeId": "92e23f68213244dfb10774c318fb3483",
 "backupKey": "bulk-backups/elkm2-prod/20260930T054246781348_92e23f68213244dfb10774c318fb3483.json",
 "succeeded": 76, "conflicts": 0, "failed": 0, "errors": []}
```

Without the dry run:

```json
{"error": {"code": "DRY_RUN_REQUIRED", "message": "Run this as a dry run first (dryRun=true) and send back its dryRunToken, expectedCount and a reason"}}
```

`_bulk_delete` is the same without `set` / `remove`; its samples show the documents that would be deleted.

### GET /api/v1/clusters/{clusterId}/data/_changes

Bulk changes on the cluster, newest first (only those on indices you can see).

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/clusters/elkm2-prod/data/_changes
```

```json
{"clusterId": "elkm2-prod", "items": [{"changeId": "92e23f68213244dfb10774c318fb3483", "op": "update",
  "index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "at": "2026-09-30T05:42:46.781Z",
  "by": "latish.madapada@fenixcommerce.com", "reason": "Hold USPS exceptions for vendor 2121", "count": 76,
  "fields": {"set": ["status"], "remove": []}, "status": "DONE",
  "result": {"succeeded": 76, "conflicts": 0, "failed": 0, "errors": []},
  "search": {"query": "vendor:2121 AND carrier:USPS", "filters": [{"field": "status", "op": "is", "value": "exception"}], "timeRange": null},
  "restoredBy": null, "restoredAt": null, "restoreOf": null}]}
```

### POST /api/v1/clusters/{clusterId}/data/_changes/{changeId}/_restore

Puts every document of a bulk change back exactly as it was before (deleted ones are recreated). Later edits to those documents are overwritten, after being backed up themselves. Same two steps: `?dryRun=true` returns `count` and a `dryRunToken`; then send `{"dryRunToken", "expectedCount", "reason"}`. Needs `edit` on every index involved.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST "http://172.0.58.49/api/v1/clusters/elkm2-prod/data/_changes/92e23f68213244dfb10774c318fb3483/_restore?dryRun=true" -d '{}'
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/clusters/elkm2-prod/data/_changes/92e23f68213244dfb10774c318fb3483/_restore \
  -d '{"dryRunToken": "<from the dry run>", "expectedCount": 76, "reason": "Undo the hold"}'
```

```json
{"applied": true, "changeId": "5d1f…", "restoreOf": "92e23f68213244dfb10774c318fb3483",
 "backupKey": "bulk-backups/elkm2-prod/…_5d1f….json", "succeeded": 76, "conflicts": 0, "failed": 0, "errors": []}
```

## Roll back any change

Every applied config change keeps the state from just before it (`config-history/…` in S3), so any past change can be undone, not only the latest one (which `…/rollback` does). Document and bulk changes keep their versions and backups (see *Changing documents*), and deleted indices keep their definition. Each rollback has a dry run, needs a `reason`, is audited, and can itself be rolled back.

### GET /api/v1/clusters/{clusterId}/config-history

Every applied change to one config resource, newest first. Needs `view` on it.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://172.0.58.49/api/v1/clusters/elkm2-prod/config-history?configType=ilm-policies&resource=delest-logs-policy"
```

```json
{"clusterId": "elkm2-prod", "configType": "ilm-policies", "resource": "delest-logs-policy", "items": [
  {"changeId": "1f0c…", "action": "UPDATE", "at": "2026-09-30T10:41:12.004Z", "by": "latish.madapada@fenixcommerce.com",
   "reason": "Back to normal", "beforeVersion": "9a1…", "afterVersion": "c47…", "restoreOf": null,
   "createdResource": false, "restorable": true}]}
```

### POST /api/v1/clusters/{clusterId}/config-history/{changeId}/_restore

Puts a config back to the state from just before change `changeId` (the `changeId` of an `UPDATE`, `ROLLBACK` or `RESTORE` audit entry). Later changes to the same resource are undone too; the dry run warns and shows the diff. If that change created the resource, restoring deletes it. Same checks as an update (`edit` on the resource, allowlist, cluster health; `?force=true` when red). Mappings are refused (`ROLLBACK_NOT_SUPPORTED`).

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST "http://172.0.58.49/api/v1/clusters/elkm2-prod/config-history/5b7e1c0a9f2d4e3a8c6b1d2e3f4a5b6c/_restore?dryRun=true" -d '{"configType": "ilm-policies", "resource": "delest-logs-policy"}'
```

```json
{"changeId": "…", "action": "RESTORE", "dryRun": true, "applied": false, "clusterId": "elkm2-prod",
 "configType": "ilm-policies", "resource": "delest-logs-policy",
 "diff": {"added": [], "removed": [], "changed": [{"path": "policy.phases.hot.actions.rollover.max_age", "before": "2d", "after": "3d"}]},
 "warnings": ["This resource changed again after that change (later changes or edits outside the API). Restoring the state from before it undoes those too: check the diff"],
 "createsResource": false, "deletesResource": false}
```

Then the same call without `dryRun` and with `"reason": "Undo the peak-season retention change"`. The audit entry is `RESTORE` with `restoreOf` = the change undone. A change made before change history existed returns `404 CHANGE_NOT_FOUND` (except the latest one, which the snapshot still has).

### POST /api/v1/clusters/{clusterId}/deleted-indices/_recreate

Recreates a deleted index **empty**, from its saved settings (minus the ones Elasticsearch manages, such as `uuid` and `creation_date`), mappings and aliases. `key` is from `GET …/deleted-indices`. Needs `edit` on the index name; refused if the name exists again (`INDEX_EXISTS`). Audited as `INDEX_RECREATE`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST "http://172.0.58.49/api/v1/clusters/elkm2-prod/deleted-indices/_recreate?dryRun=true" -d '{"key": "deleted-indices/elkm2-prod/scratch-returns-2024.09/2026-09-30T104112.293Z_fffbd704dcb74d0d9c3304ef92660085.json"}'
```

```json
{"index": "scratch-returns-2024.09", "dryRun": true, "docsLost": 1, "deletedBy": "latish.madapada@fenixcommerce.com",
 "deletedAt": "2026-09-30T10:41:12.293Z",
 "body": {"settings": {"index": {"number_of_shards": "1", "number_of_replicas": "0"}},
          "mappings": {"properties": {"sku": {"type": "keyword"}}}},
 "warnings": ["The index is recreated EMPTY: its documents were deleted and can't come back. Reindex or reload the data from its source"]}
```

### GET /api/v1/clusters/{clusterId}/data/{index}/_recent

The newest document and bulk changes on an index or pattern (`limit`, default 10, max 50), from the indices you may view. Each item says how to roll it back and whether you may (`canRollBack`: `edit` on its index); `rolledBack` is true when a later change in the list undid it. Roll back a `doc` item with `POST …/_doc/{id}/_restore` and its `versionKey`, a `bulk` item with `POST …/data/_changes/{changeId}/_restore`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://172.0.58.49/api/v1/clusters/elkm2-prod/data/shoppremiumoutlets.myshopify.com-shipment_summary-2024.09/_recent"
```

```json
{"clusterId": "elkm2-prod", "target": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09", "items": [
  {"kind": "bulk", "changeId": "92e2…", "action": "BULK_UPDATE", "op": "update", "index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09",
   "at": "2026-09-30T10:41:14.781Z", "by": "latish.madapada@fenixcommerce.com", "reason": "Hold USPS exceptions for vendor 2121",
   "count": 313, "fields": {"set": ["status"], "remove": []}, "rolledBack": false, "canRollBack": true},
  {"kind": "doc", "changeId": "a09c…", "action": "UPDATE", "id": "5138593540626", "index": "shoppremiumoutlets.myshopify.com-shipment_summary-2024.09",
   "at": "2026-09-30T10:41:13.308Z", "by": "latish.madapada@fenixcommerce.com", "reason": "Carrier confirmed delivery",
   "fields": ["status"], "versionKey": "doc-versions/elkm2-prod/…/5138593540626/…_a09c….json", "rolledBack": true, "canRollBack": true}]}
```

A bulk change can now also be undone when it was itself a restore (`POST …/_changes/{changeId}/_restore` on the restore's `changeId`): documents it recreated are deleted again.

## Admin API

Admins only (users with `admin: true`, or listed in `BOOTSTRAP_ADMINS`). Every change made here is written to the audit log.

### Clusters: where they come from

A cluster is registered in one of two ways, and both can be used together:

- **Added here** (`POST /api/v1/admin/clusters`, or **Administration → Clusters** in the console). The connection details go to `data/clusters.managed.yaml` on the EC2 server; the password or API key goes to AWS Secrets Manager as `es-config-api/clusters/<id>`. Never in S3. Takes effect at once, no restart.
- **Listed in `config/clusters.yaml`** on the server, with the password in the same kind of secret (store it with `python -m app.cli set-cluster-secret <id>`). These show `"source": "file"`, `"editable": false`, and can only be changed in the file (then `docker compose up -d`).

Every cluster in the responses below also has `"credentials": {"store": "secrets-manager", "secretName": "es-config-api/clusters/<id>", "present": true}` (`present: false` = the secret is missing).

Passwords and API keys are write-only: no endpoint returns them. Before saving, the API connects to the cluster with the new settings and refuses if it can't (`CLUSTER_TEST_FAILED`); add `?skipTest=true` to save a cluster that is down right now. Use the `config_api` service account (role `config_api_writer`, see `docs/es-lockdown.md`), not `elastic`.

**Cluster body** (POST and PUT)

| Field | Required | Notes |
| --- | --- | --- |
| `id` | POST only | Lowercase letters, digits, `-` and `_`; used in URLs; can't be changed later |
| `name`, `description` | no | `name` defaults to the id |
| `hosts` | yes | One or more node URLs, `http://` or `https://`, e.g. `https://10.0.1.10:9200` |
| `auth.type` | no | `basic` (default), `api_key` or `none` (no authentication, for clusters with security off) |
| `auth.username`, `auth.password` | basic | **The password is optional**: without one (and none saved) the cluster is saved as `none`. On PUT, leave `password` out to keep the saved one (same username) |
| `auth.apiKey` | api_key | Base64 `id:key` form. On PUT, leave out to keep the saved one |
| `verifyCerts` | no | Default `true`. Turn off only for a trusted network with self-signed certs |
| `caCertPem` | no | PEM CA certificate for HTTPS. On PUT: leave out to keep, `""` to remove |
| `requestTimeout` | no | Seconds, 5–300, default 30 |
| `tags` | no | List of labels |

### GET /api/v1/admin/clusters

Lists every cluster (added here and from `clusters.yaml`) and pings each one in parallel. Add `?check=false` to skip the ping. `managedFile` is where added clusters are saved (`null` when adding clusters is turned off).

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://172.0.58.49/api/v1/admin/clusters
```

```json
{"items": [
  {"id": "elkm2-prod", "name": "ELK M2 Production", "description": "", "tags": [],
   "hosts": ["http://node4.elkm2.prod.int.fenixcommerce.com:9200"], "authType": "basic", "username": "es_console_api",
   "verifyCerts": true, "hasCaCert": false, "requestTimeout": 30, "source": "file", "editable": false,
   "reachable": true, "version": "8.17.1", "clusterName": "alpha-elkm-cluster-1", "clusterUuid": "hmvz4z0AQP-69TruOsjhzA",
   "health": "green", "numberOfNodes": 3},
  {"id": "elkm2-staging", "name": "ELK M2 Staging", "description": "Staging logs cluster", "tags": ["staging"],
   "hosts": ["http://node1.elkm2.stage.int.fenixcommerce.com:9200"], "authType": "basic", "username": "es_console_api",
   "verifyCerts": true, "hasCaCert": false, "requestTimeout": 30, "source": "managed", "editable": true,
   "createdAt": "2026-09-29T11:43:23.690Z", "createdBy": "latish.madapada@fenixcommerce.com",
   "updatedAt": "2026-09-29T11:43:23.690Z", "updatedBy": "latish.madapada@fenixcommerce.com",
   "reachable": true, "version": "8.17.1", "clusterName": "elkm2-staging", "health": "yellow", "numberOfNodes": 1}
 ],
 "managedFile": "/app/data/clusters.managed.yaml"}
```

A cluster that can't be reached shows `"reachable": false` with `error` and `errorCode` (`CLUSTER_UNREACHABLE`, `ES_AUTH_FAILED`…) instead of the version and health.

### GET /api/v1/admin/clusters/{clusterId}

One cluster, same fields as above. Add `?check=true` to ping it.

```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://172.0.58.49/api/v1/admin/clusters/elkm2-staging?check=true"
```

### POST /api/v1/admin/clusters/test

Tries connection settings without saving anything. Same body as POST (no `id` needed). To re-test a saved cluster without re-typing its password, add its `"id"`: a missing password or API key is then taken from the saved one.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/admin/clusters/test -d '{"hosts": ["http://node1.elkm2.stage.int.fenixcommerce.com:9200"], "auth": {"type": "basic", "username": "es_console_api", "password": "…"}}'
```

```json
{"reachable": true, "version": "8.17.1", "clusterName": "elkm2-staging", "clusterUuid": "hmvz4z0AQP-69TruOsjhzA", "health": "green", "numberOfNodes": 1}
```

Wrong password:

```json
{"reachable": false, "error": "Elasticsearch refused the API's credentials: unable to authenticate user [es_console_api] for REST request [/]", "errorCode": "ES_AUTH_FAILED"}
```

### POST /api/v1/admin/clusters

Adds a cluster. Returns 201 with the saved cluster (no secrets) and the connection test. The cluster is immediately in `GET /api/v1/clusters` for admins; give other users access with `PUT /api/v1/admin/users/{email}/permissions`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://172.0.58.49/api/v1/admin/clusters -d '{
  "id": "elkm2-staging",
  "name": "ELK M2 Staging",
  "description": "Staging logs cluster",
  "hosts": ["http://node1.elkm2.stage.int.fenixcommerce.com:9200"],
  "auth": {"type": "basic", "username": "es_console_api", "password": "…"},
  "tags": ["staging"]
}'
```

```json
{"cluster": {"id": "elkm2-staging", "name": "ELK M2 Staging", "description": "Staging logs cluster", "tags": ["staging"],
  "hosts": ["http://node1.elkm2.stage.int.fenixcommerce.com:9200"], "authType": "basic", "username": "es_console_api",
  "verifyCerts": true, "hasCaCert": false, "requestTimeout": 30, "source": "managed", "editable": true,
  "createdAt": "2026-09-29T11:43:23.690Z", "createdBy": "latish.madapada@fenixcommerce.com",
  "updatedAt": "2026-09-29T11:43:23.690Z", "updatedBy": "latish.madapada@fenixcommerce.com"},
 "test": {"reachable": true, "version": "8.17.1", "clusterName": "elkm2-staging", "health": "green", "numberOfNodes": 1}}
```

HTTPS with your own CA: add `"hosts": ["https://…:9200"]` and `"caCertPem": "-----BEGIN CERTIFICATE-----\nMIID…\n-----END CERTIFICATE-----\n"` (newlines as `\n`). With an API key: `"auth": {"type": "api_key", "apiKey": "VnVhQ2ZH…"}`. A cluster without security: `"auth": {"type": "none"}` (or `basic` with no password); its `credentials` show `{"store": "none"}`.

If the cluster can't be reached, nothing is saved:

```json
{"error": {"code": "CLUSTER_TEST_FAILED", "message": "Could not connect: Could not reach cluster: Connection error. Fix the settings, or save anyway with skipTest=true",
  "details": {"reachable": false, "error": "Could not reach cluster: Connection error", "errorCode": "CLUSTER_UNREACHABLE"}}}
```

Save it anyway (for example a cluster that's being built): `POST "http://172.0.58.49/api/v1/admin/clusters?skipTest=true"`.

### PUT /api/v1/admin/clusters/{clusterId}

Changes a cluster added here. Send the full body without `id`; leave `auth.password` / `auth.apiKey` / `caCertPem` out to keep the saved values. The connection is tested first (same `?skipTest=true`).

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/clusters/elkm2-staging -d '{
  "name": "ELK M2 Staging",
  "description": "Staging logs cluster (2 nodes)",
  "hosts": ["http://node1.elkm2.stage.int.fenixcommerce.com:9200", "http://node2.elkm2.stage.int.fenixcommerce.com:9200"],
  "auth": {"type": "basic", "username": "es_console_api"},
  "requestTimeout": 60,
  "tags": ["staging"]
}'
```

Returns `{"cluster": {…}, "test": {…}}` like POST. A cluster from `clusters.yaml` is refused:

```json
{"error": {"code": "CLUSTER_READ_ONLY", "message": "Cluster 'elkm2-prod' is defined in clusters.yaml on the server; change it there"}}
```

### DELETE /api/v1/admin/clusters/{clusterId}

Removes a cluster added here. `confirm` must repeat the id. It is also removed from every user's access, and its allowlist override is deleted. Its snapshots and audit history stay in S3, so adding it back later under the same id picks them up again.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -X DELETE "http://172.0.58.49/api/v1/admin/clusters/elkm2-staging?confirm=elkm2-staging"
```

```json
{"deleted": "elkm2-staging", "removedFromUsers": ["priya@fenixcommerce.com"], "note": "Snapshots and audit history for this cluster stay in S3."}
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

Changes only the user's cluster access and keeps the admin flag. The body is the full new access map: per cluster (or `*`) either a level, or a default plus index rules.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/users/priya@fenixcommerce.com/permissions -d '{"elkm2-prod": "view"}'

# index level: view everything, edit the shipment indices, never see payments
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/users/priya@fenixcommerce.com/permissions -d '{
  "elkm2-prod": {"default": "view", "indices": [
    {"pattern": "shoppremiumoutlets*-shipment_summary-*", "level": "edit"},
    {"pattern": "payments-*", "level": "none"}]}
}'

# only some indices, nothing else on the cluster (no templates, ILM, pipelines)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X PUT http://172.0.58.49/api/v1/admin/users/dev@fenixcommerce.com/permissions -d '{
  "elkm2-prod": {"default": null, "indices": [{"pattern": "shoppremium*", "level": "edit"}, {"pattern": "delest-log-*", "level": "view"}]}
}'
```

Patterns use `*` and `?`, can't start with a dot and can't contain commas; at most 100 rules per cluster. The most specific match wins (an exact name beats a pattern; a longer literal part beats a shorter one). A cluster with no default and no rules is left out.

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

Returns the audit events for one UTC day, newest first. Filter by `clusterId`, `user` or `action` (`UPDATE`, `ROLLBACK`, `DRY_RUN`, `ADMIN_USER_UPDATE`, `ADMIN_PERMISSIONS_UPDATE`, `ADMIN_USER_DELETE`, `ADMIN_ALLOWLIST_UPDATE`, `ADMIN_ALLOWLIST_DELETE`, `ADMIN_USER_CREATE`, `ADMIN_PASSWORD_RESET`, `ADMIN_CLUSTER_CREATE`, `ADMIN_CLUSTER_UPDATE`, `ADMIN_CLUSTER_DELETE`, `INDEX_DELETE`, `DATA_SEARCH`, `DATA_DOCUMENT`, `DATA_EXPORT`, `DATA_DOC_CREATE`, `DATA_DOC_UPDATE`, `DATA_DOC_DELETE`, `DATA_DOC_RESTORE`, `DATA_BULK_UPDATE`, `DATA_BULK_DELETE`, `DATA_BULK_RESTORE`, `RESTORE`, `INDEX_RECREATE`, `AUTH_LOGIN`, `AUTH_LOGOUT`, `AUTH_LOGOUT_ALL`, `AUTH_PASSWORD_CHANGE`); `limit` defaults to 200.

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
| 400 | `INVALID_ALLOWLIST`, `INVALID_PERMISSIONS`, `UNKNOWN_CLUSTER` | Admin body is wrong: patterns must be lists, levels `view`/`edit`/`delete`, cluster ids must exist (see `GET /api/v1/admin/clusters`) |
| 400 | `INVALID_EMAIL` | New users are added by email address |
| 400 | `WEAK_PASSWORD` | New password needs 12+ characters with letters and digits (or 20+ of anything), must not be your email and must differ from the current one |
| 400 | `CANNOT_DELETE_SELF` | An admin can't delete their own user |
| 400 | `DRY_RUN_REQUIRED`, `DRY_RUN_EXPIRED`, `DRY_RUN_MISMATCH`, `COUNT_MISMATCH` | Bulk change: run the dry run first and send its `dryRunToken` and `expectedCount` with the identical body, within 15 minutes |
| 400 | `INVALID_FIELD`, `NOTHING_TO_CHANGE`, `DOCUMENT_TOO_LARGE` | Bulk update needs `set` and/or `remove` with real field paths (not `_id`…); documents over 5 MB can't be edited here |
| 400 | `INVALID_FILTER` | Data search: a filter is missing its value (or both ends of a range) |
| 400 | `RESULT_WINDOW_EXCEEDED` | Data search: `from + size` over 10,000; narrow the search or change the sort |
| 400 | `INVALID_CLUSTER` | Cluster body is wrong: bad id, node URL, a password without a username, a missing API key, or unreadable `caCertPem` |
| 400 | `CONFIRMATION_MISMATCH` | Cluster remove: `confirm` must be the exact cluster id |
| 401 | `NOT_AUTHENTICATED` | No token sent: sign in with `POST /api/v1/auth/login` and send `Authorization: Bearer $TOKEN` (in /docs: **Authorize**) |
| 401 | `SESSION_EXPIRED` | Token expired (after 12 hours) or ended by a password change, reset or logout-all; sign in again |
| 401 | `INVALID_CREDENTIALS` | Wrong email or password (sign-in or change-password) |
| 403 | `CSRF_CHECK_FAILED` | Browser-cookie write without the `X-Requested-With` header; scripts should use the Bearer token instead |
| 403 | `PERMISSION_DENIED` | Your level on that cluster is too low (`edit` to change, `delete` to delete indices) |
| 403 | `NOT_ALLOWLISTED` | A key, resource or index isn't on the allowlist; `details.blocked` lists which |
| 403 | `ADMIN_REQUIRED` | Admin-only endpoint |
| 403 | `SYSTEM_INDEX` | Data browser: names and patterns starting with `.` (system and hidden indices) can't be browsed |
| 404 | `CLUSTER_NOT_FOUND`, `INDEX_NOT_FOUND`, `RESOURCE_NOT_FOUND`, `USER_NOT_FOUND` | Check the id or name |
| 404 | `VERSION_NOT_FOUND`, `CHANGE_NOT_FOUND` | That saved version, bulk change or config change doesn't exist (or belongs to another document or resource); config changes from before change history existed can't be restored |
| 404 | `TOMBSTONE_NOT_FOUND` | No saved definition under that deleted-index key |
| 404 | `DOCUMENT_NOT_FOUND` | Data browser: no document with that id in that index |
| 404 | `NO_SNAPSHOT` | Nothing to roll back yet: no change has been made through the API |
| 409 | `DOCUMENT_CHANGED` | The document was edited by someone else since you read it; reload it and try again |
| 409 | `INDEX_EXISTS` | Recreate: an index, alias or data stream with that name exists again |
| 409 | `DOCUMENT_EXISTS` | A document with that id already exists; edit it instead |
| 409 | `COUNT_CHANGED` | Documents matching the bulk change changed since the dry run; run it again |
| 409 | `CLUSTER_EXISTS` | That cluster id is taken (by an added cluster or one in `clusters.yaml`) |
| 409 | `CLUSTER_READ_ONLY` | The cluster is defined in `clusters.yaml`; change it in that file on the server |
| 409 | `CLUSTER_MANAGEMENT_DISABLED` | `MANAGED_CLUSTERS_FILE` is empty, so clusters can only come from `clusters.yaml` |
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
| 422 | `TOO_MANY_DOCUMENTS` | A bulk change matched more than 10,000 documents; narrow the query or filters |
| 422 | `CLUSTER_TEST_FAILED` | Add/edit cluster: the API couldn't connect with those settings; `details` says why. Fix them, or add `?skipTest=true` |
| 423 | `ACCOUNT_LOCKED` | 5 wrong passwords in a row: wait 15 minutes (`details.retryAfterMinutes`) or ask an admin to reset the password |
| 500 | `SECRETS_ACCESS_DENIED` | The EC2 role may not read or write the secret: add the Secrets Manager statement from `docs/iam-policy.json` |
| 500 | `CLUSTERS_FILE_NOT_WRITABLE` | The container can't write `data/` on the server: run `sudo chown 10001:10001 data && chmod 700 data` in the project folder |
| 502 | `CLUSTER_UNREACHABLE` | The API can't reach Elasticsearch (network or security group) |
| 502 | `ES_AUTH_FAILED` | Wrong Elasticsearch username or password (in `.env`, or saved for an added cluster: fix it under **Administration → Clusters**) |
| 502 | `ES_WRITE_NOT_ALLOWED` | The API's Elasticsearch account lacks the `write` index privilege; add it to `config_api_writer` (`docs/es-lockdown.md`) |
| 502 | `CLUSTER_CREDENTIALS_MISSING` | No password / API key for the cluster: create `es-config-api/clusters/<id>` with `python -m app.cli set-cluster-secret <id>` |
| 502 | `SECRETS_UNAVAILABLE` | AWS Secrets Manager couldn't be reached (network, VPC endpoint) |
| 502 | `ES_READ_NOT_ALLOWED` | Data browser: the API's Elasticsearch account lacks the `read` index privilege; add it to `config_api_writer` (`docs/es-lockdown.md`) |
| 502 | `ES_UNAVAILABLE` | Elasticsearch returned a 5xx or 429; retry, and check the cluster |

Every response carries an `X-Request-ID` header; quote it when reporting a problem, since the same id is in the audit log.
