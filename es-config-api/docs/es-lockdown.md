# Locking down direct config writes

The API detects changes made outside it (drift), but the real protection is making
sure only the API *can* change config. Run these once on **each** cluster, from Kibana
Dev Tools or `curl`, as a superuser.

## 1. Role for the API's service account

```http
PUT _security/role/config_api_writer
{
  "cluster": [
    "monitor",
    "manage",
    "manage_ilm",
    "manage_index_templates",
    "manage_pipeline"
  ],
  "indices": [
    {
      "names": ["*"],
      "privileges": ["monitor", "view_index_metadata", "manage"],
      "allow_restricted_indices": false
    }
  ]
}
```

`manage` on the cluster is what `PUT _cluster/settings` requires. If you only want the
API to touch some indices, narrow `names` (for example `["logs-*", "products-*"]`); the
API's allowlist is a second, independent filter on top.

## 2. The service account itself

```http
POST _security/user/config_api
{
  "password": "<long random password>",
  "roles": ["config_api_writer"],
  "full_name": "ES Config API service account"
}
```

Or, with API keys:

```http
POST _security/api_key
{
  "name": "es-config-api",
  "role_descriptors": { "config_api_writer": { ...same body as the role above... } }
}
```

Put the password or the `encoded` API key in the EC2 `.env` file and reference it from
`clusters.yaml`.

## 3. Read-only role for people

```http
PUT _security/role/config_reader
{
  "cluster": ["monitor", "read_ilm", "read_pipeline"],
  "indices": [
    { "names": ["*"], "privileges": ["read", "view_index_metadata", "monitor"] }
  ]
}
```

Assign `config_reader` (plus whatever Kibana feature privileges they need) to the people
who use Kibana, and **remove** `superuser`, `manage`, `manage_ilm`,
`manage_index_templates`, `manage_pipeline` and index `manage` from their existing roles.
They can still inspect everything in Dev Tools; changes go through the API.

Keep one break-glass superuser whose credentials are stored securely and whose use is
reviewed. Changes made with it show up as drift in the API.

## 4. Check it worked

As a person with `config_reader`:

```http
PUT _cluster/settings
{ "persistent": { "indices.recovery.max_bytes_per_sec": "50mb" } }
```

should return `403 security_exception ... action [cluster:admin/settings/update] is unauthorized`.
