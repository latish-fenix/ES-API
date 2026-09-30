# ES Config Console — Fresh install on EC2

*30 September 2026. The same guide, kept in sync, is also a shared Claude Doc.*

This guide installs the ES Config Console on the EC2 server from scratch. It first deletes the old installation: its S3 data, its users, its secrets and the old Elasticsearch accounts. Nothing old is reused. You end with one admin (you) and no clusters, then add clusters and people in the console.

## Before you start

| You need | For |
| --- | --- |
| The latest code pushed to GitHub (`latish-fenix/ES-API`) from `Desktop\ES-API` | EC2 pulls it from there |
| A shell on the EC2 server (`172.0.58.49`) with `sudo`, Docker and the AWS CLI | Running the steps |
| AWS admin access (AWS console or CloudShell) | S3, IAM role and Secrets Manager; the EC2 role itself can't delete old data |
| A superuser login on each Elasticsearch cluster (Kibana Dev Tools or `curl`) | Creating the new service account and removing the old ones |
| About 30 minutes | |

Push the code first, on your PC in Command Prompt:

```bat
cd %USERPROFILE%\Desktop\ES-API
git add -A
git commit -m "Secrets Manager, index access, document edits, optional cluster passwords"
git push
```

In the steps below, replace the values in `< >`. The examples use the bucket `fenix-es-config-api`, the prefix `es-config-api/`, the region `us-east-1` and the new Elasticsearch account `es_console_api`.

## Step 1: Stop the old installation and delete its data

This removes the old users (including the demo ones), snapshots, audit log, allowlist, clusters and passwords. **It can't be undone.**

1. On EC2, note where the old server kept its data, then stop it:

   ```bash
   cd ~/ES-API/es-config-api
   grep -E '^(S3_BUCKET|S3_PREFIX|AWS_REGION|SECRETS_PREFIX)=' .env
   docker compose down
   ```

2. Delete the old S3 data. Run this in **AWS CloudShell** (or with admin credentials): the EC2 role is not allowed to delete. Use the bucket and prefix printed above.

   ```bash
   BUCKET=fenix-es-config-api
   PREFIX=es-config-api/
   aws s3 ls "s3://$BUCKET/$PREFIX"                 # look before you delete
   aws s3 rm "s3://$BUCKET/$PREFIX" --recursive
   ```

   With bucket versioning on, S3 keeps the old versions as hidden copies. The API never reads them; to remove them too, add a lifecycle rule on that prefix that expires noncurrent versions after 1 day.

3. Delete any old secrets under the prefix (also in CloudShell). The old server kept its secrets in `.env`, so this usually finds nothing:

   ```bash
   REGION=us-east-1
   for s in $(aws secretsmanager list-secrets --region $REGION \
       --filters Key=name,Values=es-config-api/ --query 'SecretList[].Name' --output text); do
     echo "deleting $s"
     aws secretsmanager delete-secret --region $REGION --secret-id "$s" --force-delete-without-recovery
   done
   ```

   Wait a minute before Step 6: a deleted secret's name takes a few seconds to become free again.

4. Delete the old settings and files on EC2 (`data/` is owned by the container user, hence `sudo`):

   ```bash
   cd ~/ES-API/es-config-api
   sudo rm -rf data
   rm -f .env config/clusters.yaml
   ls certs 2>/dev/null     # keep only CA certificates you still need; delete the rest
   docker image rm es-config-api:latest 2>/dev/null || true
   ```

## Step 2: Get the latest code on EC2

```bash
cd ~/ES-API
git status --short        # should list nothing; if it lists files, run: git checkout -- .
git pull
cd es-config-api
ls app/secret_store.py docs/FRESH_INSTALL.md   # both must exist, or the pull didn't get the new code
```

No clone yet on this server? `cd ~ && git clone https://github.com/latish-fenix/ES-API.git`, then continue in `~/ES-API/es-config-api`.

## Step 3: Give the EC2 role access to S3 and Secrets Manager

The API stores its data in S3 and every password, key and hash in AWS Secrets Manager, using the EC2 instance's IAM role (no AWS keys on the server).

1. **S3 bucket** (keep the existing one, or create `fenix-es-config-api`): Block Public Access on, versioning on, default encryption on.
2. **Policy**: on EC2, fill in the account id and print the policy:

   ```bash
   cd ~/ES-API/es-config-api
   aws sts get-caller-identity --query Arn --output text   # shows the role name: assumed-role/<ROLE>/...
   ACCOUNT_ID=<your 12-digit account id>
   sed "s/ACCOUNT_ID/$ACCOUNT_ID/g" docs/iam-policy.json
   ```

   If your bucket, prefix or region differ from `fenix-es-config-api`, `es-config-api/` and `us-east-1`, change them in the output too. Delete the last statement (`KmsIfBucketOrSecretsUseACustomerManagedKey`) unless the bucket or secrets use your own KMS key.
3. **Attach it**: AWS console → IAM → Roles → the role from above → **Add permissions → Create inline policy → JSON**, paste, name it `es-config-api`, save. Replace any older `es-config-api` policy on that role.
4. **No internet on the instance?** Add a VPC interface endpoint for `secretsmanager` and a gateway endpoint for `s3` in its VPC.

The policy allows only the bucket prefix and secrets named `es-config-api/*`, and allows S3 deletes only for locks and allowlist overrides.

## Step 4: Create a new Elasticsearch account and remove the old ones

Do this on **each cluster** that has security turned on, in Kibana → Dev Tools as a superuser. A cluster with security off needs nothing here: you add it later without a password.

1. Make a new password for the account (on EC2, or any machine). Keep it only until Step 7, where you paste it once:

   ```bash
   openssl rand -base64 24
   ```

2. Create (or update) the role and the new account `es_console_api`:

   ```http
   PUT _security/role/config_api_writer
   {
     "cluster": ["monitor", "manage", "manage_ilm", "manage_index_templates", "manage_pipeline"],
     "indices": [
       {"names": ["*"], "privileges": ["monitor", "view_index_metadata", "manage", "read", "write"],
        "allow_restricted_indices": false}
     ]
   }

   POST _security/user/es_console_api
   {
     "password": "<the new password>",
     "roles": ["config_api_writer"],
     "full_name": "ES Config Console service account"
   }
   ```

3. Remove the old accounts. `config_api` and `reader` are the two demo users the earlier setup created; remove `config_api` only if nothing else uses it:

   ```http
   GET _security/user/config_api,reader

   DELETE _security/user/config_api
   DELETE _security/user/reader
   DELETE _security/role/config_reader
   ```

   A `404` or `"found": false` means it didn't exist; that's fine.

No Kibana? The same calls work with `curl -u elastic` from EC2, for example `curl -u elastic -X DELETE http://<node>:9200/_security/user/reader` (it asks for the `elastic` password).

## Step 5: Write the new configuration

```bash
cd ~/ES-API/es-config-api
cp .env.example .env
nano .env
```

In `.env`, set these lines and leave the rest as they are:

| Line | Value | Why |
| --- | --- | --- |
| `API_PORT` | `80` | The console is at `http://172.0.58.49/ui/` |
| `S3_BUCKET` | `fenix-es-config-api` (your bucket) | Where the API keeps its data |
| `S3_PREFIX` | `es-config-api/` | Folder in the bucket (the one emptied in Step 1) |
| `AWS_REGION` | `us-east-1` | Region of the bucket and the secrets |
| `SECRETS_BACKEND` | `aws` | Every secret in AWS Secrets Manager |
| `SECRETS_PREFIX` | `es-config-api/` | Secret names start with this |
| `BOOTSTRAP_ADMINS` | `latish.madapada@fenixcommerce.com` | The first admin (comma-separate several) |

`.env` holds no passwords: don't add `SESSION_SECRET`, `BOOTSTRAP_ADMIN_PASSWORD` or any Elasticsearch password. Then create an empty cluster list (clusters are added in the console) and the data folder:

```bash
printf 'clusters: []\n' > config/clusters.yaml
mkdir -p certs data
sudo chown 10001:10001 data && sudo chmod 700 data
grep -nE '^[A-Z_]*(PASSWORD|SECRET|API_KEY)=' .env   # must print nothing
```

## Step 6: Start it and get the first password

```bash
cd ~/ES-API/es-config-api
docker compose up -d --build          # builds the API and the web console, a few minutes
docker compose logs --tail 40 es-config-api
curl -s localhost/healthz              # {"status":"ok","clusters":0}
```

On this first start the API creates the secret `es-config-api/app` with a random session key and a random first-admin password, and gives that password to the `BOOTSTRAP_ADMINS` email. The log says `Generated the first-admin password`.

Read the password (on EC2; the role is allowed to read its own secrets):

```bash
aws secretsmanager get-secret-value --region us-east-1 --secret-id es-config-api/app \
  --query SecretString --output text \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["bootstrapAdminPassword"])'
```

Or in the AWS console: Secrets Manager → `es-config-api/app` → **Retrieve secret value** → `bootstrapAdminPassword`.

Check that the secrets exist:

```bash
docker compose exec es-config-api python -m app.cli secrets-status
# present   es-config-api/app
# present   es-config-api/users/latish.madapada@fenixcommerce.com
```

## Step 7: Sign in and set it up in the console

1. **Sign in**: open `http://172.0.58.49/ui/`, sign in with your email and the password from Step 6.
2. **Change your password**: **Change password** at the bottom left (at least 12 characters). The first password stops working.
3. **Add each cluster**: **Administration → Clusters → Add cluster**.
   - Cluster id (for example `elkm2-prod`), display name, node URLs (one per line, with `http://` or `https://` and `:9200`).
   - **Username and password**: `es_console_api` and the password from Step 4. It goes straight to Secrets Manager as `es-config-api/clusters/<id>` and is never shown again.
   - **Cluster without security**: leave the password empty (or pick **No password**). It is saved without authentication.
   - **Test connection** → green → **Add cluster**.
4. **Open up what people may change**: **Administration → Allowlist**. Every config type starts locked; unlock the types and add the name patterns you want (see the User Guide).
5. **Add people**: **Administration → Users → Add users**. Enter emails, pick each person's level per cluster (and index rules if needed), **Create**, then **Download CSV** and hand each person their password. Delete the CSV afterwards.

You now have one admin, your clusters and your people, and nothing from the old installation.

## Checks and troubleshooting

| Check | Expect |
| --- | --- |
| `curl -s localhost/healthz` on EC2 | `{"status":"ok",...}` |
| **Administration → Users** | Only you, until you add people |
| **Administration → Clusters → Details** | Credentials: "In AWS Secrets Manager · es-config-api/clusters/&lt;id&gt;" (or "Not needed" for a cluster without a password) |
| `grep -i password .env config/clusters.yaml`, `sudo grep -i password data/clusters.managed.yaml` | Only comment lines from .env; nothing from the cluster files |
| Overview | Nodes with CPU, RAM, heap and disk |

| Problem | Fix |
| --- | --- |
| Log or page says `SECRETS_ACCESS_DENIED` | The EC2 role lacks the Secrets Manager statement (Step 3) |
| `SECRETS_UNAVAILABLE`, or start hangs | No network path to Secrets Manager: add the VPC endpoint (Step 3) |
| Start fails with "already scheduled for deletion" | A secret deleted in Step 1 isn't free yet: wait a minute, `docker compose restart` |
| Sign-in refused with the Step 6 password | You already changed it, or another admin did. On EC2: `docker compose exec es-config-api python -m app.cli reset-password <email>` prints a new one |
| Add cluster: "Can't connect", `ES_AUTH_FAILED` | Wrong username or password, or the cluster has security on and you left the password empty |
| Add cluster: `CLUSTERS_FILE_NOT_WRITABLE` | `sudo chown 10001:10001 data && sudo chmod 700 data`, then `docker compose restart` |
| Data page: `ES_READ_NOT_ALLOWED` / `ES_WRITE_NOT_ALLOWED` | The role in Step 4 misses `read` / `write` on indices |
| Port 80 already in use | Stop what uses it (`sudo ss -ltnp 'sport = :80'`) or set another `API_PORT` |

## Fresh start on your PC (local test)

The local test scripts no longer create demo users or use built-in passwords. For a clean start: close the Elasticsearch and API windows, delete `%USERPROFILE%\es-local`, and in `Desktop\ES-API\es-config-api\local-test` delete `.store`, `.secrets` and `clusters.managed.yaml`. Then run `1-start-elasticsearch.cmd` and `2-start-api.cmd` again. New random passwords are generated into `local-test\.secrets\local-passwords.json`, and the API window prints your first sign-in password. The only Elasticsearch account created is the API's own, `es_console_api`.
