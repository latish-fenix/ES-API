# ES Config API

Get, update, dry-run and roll back configuration on self-managed Elasticsearch 8.17
clusters, one cluster per request. Every change is snapshotted to S3 before it is
applied, and every action is written to an audit log in S3.

- **Config types:** cluster settings (persistent), index settings (dynamic), index
  mappings (add-only), index templates, component templates, ILM policies, ingest pipelines
- **Rollback:** one level back; rollback *swaps*, so rolling back twice re-applies the change.
  Mappings can't be rolled back (Elasticsearch doesn't allow removing fields)
- **Safety:** per-user, per-cluster permissions; a settings allowlist; a lock per resource;
  drift detection; a cluster-health gate; `?dryRun=true` on every change
- **Storage:** S3 only (no database). Conditional writes keep concurrent changes safe

Interactive docs: `http://<host>:8080/docs`

---

## Quick start on EC2

1. **S3 bucket**: create a bucket (for example `fenix-es-config-api`) with versioning on,
   Block Public Access on, and default encryption (SSE-S3 or SSE-KMS).
2. **IAM role**: attach a role to the EC2 instance with `docs/iam-policy.json`, after
   replacing the bucket name, prefix and KMS key.
3. **Elasticsearch**: create the `config_api` service account on each cluster, and lock
   down direct writes for people, following `docs/es-lockdown.md`.
4. **Configure** on the EC2 host:
   ```bash
   cp .env.example .env                                   # S3 settings, admins, ES creds
   cp config/clusters.example.yaml config/clusters.yaml   # your clusters
   mkdir -p certs && cp /path/to/ca.crt certs/            # CA certs referenced in clusters.yaml
   ```
5. **Run**
   ```bash
   docker compose up -d --build
   curl localhost:8080/healthz
   ```
6. **Restrict network access**: allow port 8080 only from trusted internal sources in
   the security group. Until real authentication is added, anyone who can reach the API
   can claim any username in `X-User`.

## First-time setup (as a bootstrap admin)

A new install blocks every change until an allowlist exists.

```bash
API=http://localhost:8080/api/v1
H='-H X-User:latish -H Content-Type:application/json'

# Allowlist: glob patterns per config type; deny wins over allow
curl $H -X PUT $API/admin/allowlist -d '{
  "cluster-settings": {"allow": ["cluster.routing.allocation.*", "indices.recovery.*"],
                       "deny":  ["cluster.routing.allocation.enable"]},
  "index-settings":   {"allow": ["index.number_of_replicas", "index.refresh_interval"],
                       "indices": ["logs-*", "products-*"]},
  "index-mappings":   {"allow": ["products-*"]},
  "index-templates":  {"allow": ["logs-*"], "indexPatterns": ["logs-*"]},
  "ilm-policies":     {"allow": ["*"]},
  "ingest-pipelines": {"allow": ["*"]}
}'

# What the patterns match:
#   cluster-settings / index-settings: allow/deny = setting keys
#   index-settings.indices (optional, default all): which indices those keys may be changed on
#   index-mappings: allow/deny = index names
#   templates, ILM, pipelines: allow/deny = resource names
#   index-templates.indexPatterns (optional): every index_pattern in the template must match one;
#     without it, templates with index_patterns "*" or ".something" are refused
# Dot (system/hidden) indices only match patterns that start with "." (e.g. ".my-app-*").
# A config type missing from the allowlist is fully locked.

# Users: "view" = read only, "edit" = change / dry-run / roll back,
#        "delete" = edit + delete indices; "*" = every cluster
curl $H -X PUT $API/admin/users/priya -d '{"clusters": {"prod-us": "edit", "staging": "edit"}}'
curl $H -X PUT $API/admin/users/sam   -d '{"clusters": {"*": "view"}}'
curl $H -X PUT $API/admin/users/sam/permissions -d '{"staging": "edit", "prod-us": "view"}'
```

## Using it

```bash
# Which clusters can I see?
curl $H $API/clusters

# Read current config (returns `version`, also sent as the ETag header)
curl $H $API/clusters/prod-us/cluster-settings

# Dry run: diff, validation, warnings; changes nothing
curl $H -X PUT "$API/clusters/prod-us/cluster-settings?dryRun=true" \
  -d '{"config": {"indices.recovery.max_bytes_per_sec": "80mb"}}'

# Apply (reason is required and audited; If-Match is optional but recommended)
curl $H -H 'If-Match: "<version from GET>"' -X PUT $API/clusters/prod-us/cluster-settings \
  -d '{"config": {"indices.recovery.max_bytes_per_sec": "80mb"},
       "reason": "Speed up recovery during node replacement"}'

# See what rollback would restore, then roll back
curl $H $API/clusters/prod-us/cluster-settings/previous
curl $H -X POST "$API/clusters/prod-us/cluster-settings/rollback?dryRun=true"
curl $H -X POST $API/clusters/prod-us/cluster-settings/rollback -d '{"reason": "Recovery done"}'
```

### Endpoints

| Config type | Get / update | Rollback | Body `config` |
| --- | --- | --- | --- |
| Cluster settings | `GET/PUT /clusters/{id}/cluster-settings` | `POST …/rollback` | partial map; `null` resets a key |
| Index settings | `GET/PUT /clusters/{id}/indices/{index}/settings` | `POST …/rollback` | partial map of dynamic settings |
| Index mapping | `GET/PUT /clusters/{id}/indices/{index}/mapping` | — (permanent) | `{"properties": {...}}`, new fields only |
| Index template | `GET/PUT /clusters/{id}/index-templates/{name}` | `POST …/rollback` | full template body |
| Component template | `GET/PUT /clusters/{id}/component-templates/{name}` | `POST …/rollback` | full body |
| ILM policy | `GET/PUT /clusters/{id}/ilm-policies/{name}` | `POST …/rollback` | `{"policy": {...}}` |
| Ingest pipeline | `GET/PUT /clusters/{id}/ingest-pipelines/{name}` | `POST …/rollback` | full body; add `sampleDocs` to dry-run on real docs |

**Deleting an index:** `DELETE /clusters/{id}/indices/{index}?dryRun=true` shows what would go.
To delete it for real, add `confirm=<index name>&reason=...`. The documents can't be recovered.
The API saves the settings, mappings and aliases to S3 under `deleted-indices/`, listed by
`GET /clusters/{id}/deleted-indices`, so an *empty* index can be recreated. Deleting needs the
`delete` permission level (`view` < `edit` < `delete`) and a matching `index-delete`
allowlist pattern. Aliases, data-stream names and a data stream's current write index are refused.

Also available: `GET /clusters/{id}/health`, `GET /clusters/{id}/indices`,
`GET /clusters/{id}/{type}` (list names), and `…/previous` (the stored snapshot) for
every type.

Every request body is `{"config": …, "reason": "…", "sampleDocs": [...]}`. Query flags:
`dryRun=true`, and `force=true` to proceed despite drift (rollback) or a red cluster.

Admin API (admins only): `/admin/clusters`, `/admin/users[/{u}[/permissions]]`,
`/admin/allowlist[/{clusterId}]`, and `/admin/audit?date=YYYY-MM-DD&clusterId=&user=&action=`.

### Error codes

| Status | Code examples |
| --- | --- |
| 400 | `INVALID_REQUEST`, `INVALID_CONFIG`, `REASON_REQUIRED`, `ES_REJECTED` |
| 401 / 403 | `MISSING_USER`, `UNKNOWN_USER`, `PERMISSION_DENIED`, `NOT_ALLOWLISTED`, `ADMIN_REQUIRED` |
| 404 | `CLUSTER_NOT_FOUND`, `RESOURCE_NOT_FOUND`, `INDEX_NOT_FOUND`, `NO_SNAPSHOT` |
| 409 | `CHANGE_IN_PROGRESS`, `DRIFT_DETECTED`, `CLUSTER_UNHEALTHY`, `CONCURRENT_CHANGE` |
| 412 | `VERSION_MISMATCH` (If-Match) |
| 422 | `STATIC_SETTING`, `READ_ONLY_SETTING`, `MAPPING_CONFLICT`, `ROLLBACK_NOT_SUPPORTED` |
| 409 | `LOCK_LOST` (change outlived its lock) |
| 502 | `CLUSTER_UNREACHABLE`, `ES_AUTH_FAILED`, `ES_UNAVAILABLE` (ES 5xx / 429) |

## How a change runs

1. Check permission, the reason, and that the type supports the action
2. Take the per-resource lock in S3 (`If-None-Match: *`, expires after `LOCK_TTL_SECONDS`)
3. Read the live config, finish or discard any interrupted change, check `If-Match`
4. Drift check: live config vs what the API last applied (update warns; rollback refuses unless forced)
5. Cluster health: red refuses unless forced; yellow warns
6. Validate, build the diff, check the allowlist; **dry run stops here** and returns the preview
7. Write the snapshot to S3 as *pending*, apply to Elasticsearch, **re-read the live
   config**, then promote the snapshot and write the audit event

The re-read decides the outcome, not the ES response: if the config is unchanged (ES
rejected it, or the body was already in effect), the pending snapshot is dropped and the
existing rollback target is kept. If it changed even though ES returned an error (such as a
timeout after the write landed), the change is recorded and a warning is returned. If the
cluster can't be read back, the pending snapshot stays and the next request settles it.

## S3 layout (under `S3_PREFIX`)

```text
snapshots/{cluster}/{type}/{resource}.json   previous config, pending change, last applied version
locks/{cluster}/{type}/{resource}.lock       lock owner + expiry
state/users.json                             users and permissions
state/allowlist/global.json                  global allowlist
state/allowlist/{cluster}.json               per-cluster override
audit/yyyy/mm/dd/{time}_{eventId}.json       one object per audit event
```

Audit events are also printed to stdout as JSON lines, for CloudWatch or any log shipper.
To make the audit log tamper-proof, turn on S3 Object Lock for `audit/`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `STORAGE_BACKEND` | `s3` | `local` = files on disk (dev only) |
| `S3_BUCKET`, `S3_PREFIX`, `AWS_REGION` | —, `es-config-api/`, — | where state lives |
| `S3_SSE` | bucket default | `aws:kms` or `AES256` to force encryption per object |
| `CLUSTERS_FILE` | `/app/config/clusters.yaml` | cluster registry |
| `BOOTSTRAP_ADMINS` | — | comma-separated admin usernames |
| `USER_HEADER` | `X-User` | header carrying the caller's username |
| `LOCK_TTL_SECONDS` | `600` | lock expiry; keep above the slowest possible request |
| `AUDIT_QUERY_LIMIT` | `500` | max events per audit query |

Adding a cluster: add it to `clusters.yaml`, add its credentials to `.env`, then run
`docker compose up -d` to restart.

## Development and tests

**Hands-on test on a Windows PC:** follow `local-test/TESTING.md`. Two `.cmd` scripts install
Elasticsearch 8.17.1, start the API, and walk you through every feature in the browser at `/docs`.

The tests run against a **real** Elasticsearch 8.17 cluster; S3 is mocked with moto.

```bash
pip install -r requirements-dev.txt
ES_TEST_URL=http://127.0.0.1:9200 ES_TEST_USER=elastic ES_TEST_PASSWORD=... pytest -q
# To prove the lockdown role is enough, run the API calls as the service account:
ES_TEST_API_USER=config_api ES_TEST_API_PASSWORD=... pytest -q
```

Run locally without AWS: `STORAGE_BACKEND=local CLUSTERS_FILE=./config/clusters.yaml
uvicorn app.main:create_app --factory --reload`

## Known limits (v1)

- `X-User` is trusted, not authenticated. Replace `resolve_username` in `app/identity.py`
  with SSO/JWT validation when ready
- One rollback level (bucket versioning keeps older snapshots for a future multi-level rollback)
- No delete endpoints for templates, policies or pipelines
- Static index settings and mapping changes to existing fields are refused (both need a
  closed index or a reindex); runtime fields can't be changed, because ES replaces them
  whole, so they aren't add-only
- `composed_of` component templates in an index template aren't checked against the allowlist
