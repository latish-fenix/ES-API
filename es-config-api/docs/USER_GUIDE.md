# ES Config Console — User Guide

*Last updated 29 September 2026. The same guide, kept in sync, is also a shared Claude Doc.*

The ES Config Console is the web page for reading and changing Elasticsearch configuration safely: every change is previewed first, saved to S3 before it is applied, and written to an audit log.

- **Address:** <http://172.0.58.49/ui/> (the internal network only; `http://172.0.58.49/` opens it too)
- **Who it's for:** anyone who changes cluster settings, index settings, mappings, templates, ILM policies or ingest pipelines, and the admins who manage who may do what
- **The one rule to remember:** every change goes **Edit → Dry run → Apply**. The dry run shows exactly what would change and touches nothing. Apply saves the current config as a snapshot first, so one click rolls it back

What you can see and do depends on your access level on each cluster (see [Finding your way](#finding-your-way)). Admins also get the Users, Allowlist and Audit log pages.

For scripts and automation, the same actions are available through the API; see [API_REFERENCE.md](API_REFERENCE.md) (also shared as the [ES Config API endpoint reference](https://claude.ai/code/artifact/21683e33-2775-49ce-ada3-c6a7ce220773)).

## Getting started

You sign in with your work email and the password an admin gave you; there is no self sign-up.

![Sign-in page](images/01-sign-in.png)

1. Open <http://172.0.58.49/ui/>.
2. Enter your email and password, then **Sign in**. Email is not case-sensitive; the password is.
3. You land on the Overview of the cluster you used last (or the first one you can access).

**Your first password** arrives from an admin as a CSV file with two columns, `username,password`. It is 16 characters and was generated for you; nobody, including the admin, can see it again.

**Change your password (optional, recommended).** Click **Change password** at the bottom of the sidebar. Enter the current password and the new one twice; the checklist turns green as each rule is met:

- at least 12 characters
- two of lowercase, uppercase and digits (or 20+ characters of anything)
- not your email address
- different from the current password

![Change password](images/19-change-password.png)

Changing your password keeps you signed in on this browser and signs you out everywhere else, including scripts that use your token.

| Situation | What happens | What to do |
| --- | --- | --- |
| Wrong password | "Wrong email or password" | Check caps lock and try again |
| 5 wrong passwords in a row | Account locked for 15 minutes | Wait, or ask an admin to reset your password (that also unlocks it) |
| Forgot your password | — | Ask an admin to reset it; you get a new CSV |
| Session expired (after 12 hours) | You are sent back to the sign-in page | Sign in again; you return to the page you were on |
| Signing out | **Sign out** at the bottom of the sidebar | Use **Sign out everywhere** on the Change password page if you think someone else has your session |

The console runs over plain HTTP on the internal network for now, so only sign in from trusted machines on that network.

## Finding your way

Everything happens on one cluster at a time: pick it in the **Cluster** box at the top of the sidebar, and every page below it shows that cluster.

![Overview page](images/02-overview.png)

- **Sidebar, Configure:** Overview, Cluster settings, Indices, Index templates, Component templates, ILM policies, Ingest pipelines
- **Sidebar, Administration** (admins only): Users, Allowlist, Audit log
- **Top bar:** where you are (click a part to go back) and the cluster's health (green, yellow or red), refreshed every minute
- **Overview:** health, number of indices and documents (system indices hidden), your access, and links to each area. Admins also see what the allowlist allows on this cluster and today's activity
- **Switching clusters** keeps you on the same kind of page (for example Indices on the other cluster)

### Access levels

An admin sets your level per cluster. Each level includes the ones above it.

| Level | You can |
| --- | --- |
| View | Read config, health, snapshots and the deleted-index list |
| Edit | Also dry run, apply and roll back changes |
| Delete | Also delete indices |
| Admin | Everything on every cluster, plus Users, Allowlist and Audit log |

Even with Edit or Delete, you can only change what the **allowlist** allows. A setting, index or policy that isn't on it is refused with "Not on the allowlist"; ask an admin if you need it.

The console follows your computer's light or dark mode. On a narrow window the sidebar folds into the menu button at the top left.

## How every change works

Every configuration screen uses the same four steps, and nothing reaches Elasticsearch until you click Apply.

![Every change: edit, dry run, apply, roll back](images/00-change-flow.png)

1. **Edit** the JSON in the editor. It checks the JSON as you type and underlines mistakes.
2. **Dry run.** The console asks Elasticsearch what would happen and shows the result. Nothing changes.
3. **Read the result** (below), give a **reason** (required; it goes into the audit log), then **Apply change**.
4. The console saves the current config to S3 as a **snapshot**, applies your change, and shows the new state. If anything goes wrong, **Roll back** restores the snapshot.

![Dry run result on Cluster settings](images/03-cluster-settings-dry-run.png)

### Reading a dry run

| You see | It means |
| --- | --- |
| Green **Dry run passed · N changes** | Valid; the table lists every key that would be Added, Changed or Removed, with before and after values |
| Blue **No change** | The cluster already has exactly this; nothing to apply |
| Red box | The change would be refused (not allowlisted, invalid value, conflict); the box says why and nothing was changed |
| Yellow **Warning** | Allowed, but read it first (for example the cluster is yellow, or the change is permanent) |
| **Creates a new resource** | The template, policy or pipeline doesn't exist yet; rolling back later deletes it again |
| "default" or "—" in a column | The key isn't set before (or after), so Elasticsearch's default applies |

Editing the JSON after a dry run cancels it; run it again before applying.

### The status badges

Each page shows three badges under its title:

- **version** — a short fingerprint of the live config. Apply only goes through if the config still has this version, so you never overwrite someone else's change made while you were editing ("The config changed since you loaded it": reload and try again)
- **No drift / Drift detected** — drift means someone changed the config directly in Elasticsearch, outside the console, since it was last applied here. The dry run warns you, and a rollback is refused unless you tick the "force" box, because it would wipe that outside change
- **Snapshot saved / No snapshot yet** — whether there is something to roll back to. **View snapshot** shows it

### Rolling back

Click **Roll back**. The dialog shows who took the snapshot, when, and a preview of exactly what the rollback changes. Give a reason and confirm.

![Rollback dialog](images/05-rollback.png)

There is one level of rollback, and it **swaps**: the config you replace becomes the new snapshot, so rolling back twice re-applies your change. Mappings can't be rolled back (Elasticsearch never removes fields).

## Cluster settings

This page manages the cluster's **persistent** settings; send only the keys you want to change.

![Cluster settings after a change](images/04-cluster-settings-applied.png)

- **Left:** the current persistent settings (filter by key; **Edit** copies one into the editor). "No persistent settings" means the cluster runs on defaults. Transient settings are not managed here; if the cluster has any, a warning says so
- **You can change** (admins): the allowlisted keys and patterns, with **Add to change** for exact keys
- **Right:** the change editor

Examples of what to type:

| Goal | Editor content |
| --- | --- |
| Change one setting | `{"indices.recovery.max_bytes_per_sec": "80mb"}` |
| Change two at once | `{"cluster.routing.allocation.node_concurrent_recoveries": 4, "indices.recovery.max_bytes_per_sec": "80mb"}` |
| Reset a setting to its default | `{"indices.recovery.max_bytes_per_sec": null}` |

A key that isn't on the allowlist, or is on its deny list, is refused at the dry run:

![A setting that isn't allowlisted](images/06-not-allowlisted.png)

## Indices

The Indices page lists every index (system indices starting with a dot are hidden) with its health, status and document count; click a name to open its settings or mapping.

![Indices list](images/07-indices.png)

Type part of a name in the filter box to narrow the list (for example `delest-log-2026.09`). Very long lists show the first 500; filter to find the rest.

### Index settings

![Index settings](images/08-index-settings.png)

Only **dynamic** settings can be changed on an open index, for example `index.refresh_interval` or `index.number_of_replicas`. Static settings such as `index.number_of_shards` are refused ("This setting can't be changed on an open index"). Index settings have a snapshot and rollback like cluster settings.

```json
{ "index.refresh_interval": "30s" }
```

### Index mapping (add-only, permanent)

The Mapping tab lists every field and its type. You can **add** fields, never change or remove one: that's an Elasticsearch rule, and it's why there is no rollback here.

![Adding a field to a mapping](images/09-mapping-add-field.png)

1. Type the new fields, for example `{"properties": {"customer_id": {"type": "keyword"}}}`.
2. **Dry run**, check the table lists only new paths, give a reason.
3. Tick **I understand this can't be removed or rolled back later**, then **Add fields permanently**.

Changing the type of an existing field is refused ("Mappings can only gain new fields"); that needs a new index and a reindex, outside this console.

### Deleting an index

Deleting destroys the index's documents for good. The console first saves its settings, mappings and aliases to S3 (not the data), so an empty copy can be recreated.

You need **Delete** access on the cluster, and the index must be on the index-delete allowlist. Click **Delete** in the list (or **Delete index** on the index page). The dialog shows the document count, size, aliases and data stream, and checks four things before it lets you continue:

![Delete dialog refusing an index that isn't allowlisted](images/10-delete-index-blocked.png)

- you have delete access on this cluster
- the index is on the index-delete allowlist
- it isn't the current write index of a data stream (roll the data stream over first)
- it's a real index, not an alias or data stream name

When all four are green, type the index name exactly, give a reason and click **Delete index**. Admins see a grey Delete with a lock in the list when an index isn't allowlisted; for everyone else the dialog's checklist says so.

The **Deleted through the API** tab lists every index deleted this way, who deleted it, when and why; **Definition** shows the saved settings and mappings as JSON you can copy.

![Deleted indices](images/11-deleted-indices.png)

## Templates, ILM policies and ingest pipelines

Index templates, component templates, ILM policies and ingest pipelines share one screen: a list on the left, the selected item's full definition on the right.

![Editing an ILM policy](images/12-ilm-dry-run.png)

- **Pick one** from the list (filter by name). Built-in items (names starting with a dot or containing `@`) are hidden until you tick **Show built-in**
- **Edit** the whole definition: what you apply replaces it completely, so keep the parts you don't change. **Reset edits** puts back what's live
- **Dry run** shows the diff, plus what Elasticsearch would do with it:
  - **Index templates:** the settings and mappings a new matching index would get, and any other templates that overlap
  - **Ingest pipelines:** each sample document after processing (see below)
- **New** creates one: enter a name, start from the example, dry run (it says "Creates a new resource"), then **Create**. Rolling back a new item deletes it

The allowlist decides which names you may change, and for index templates which `index_patterns` they may target. Templates matching every index (`*`) or system indices are refused unless the allowlist explicitly allows them.

### Ingest pipelines: test with sample documents

Put one or more example documents in the **Sample documents** box before the dry run to see exactly what the pipeline does to them.

![Pipeline dry run with a sample document](images/13-pipeline-sample-docs.png)

### ILM policies: be careful with the delete phase

An ILM policy is a schedule Elasticsearch follows on its own: for example roll over to a new index every day, then delete indices older than 30 days. A policy change applies to **every index that uses it** on ILM's next run.

- Shortening a delete phase (say `min_age` from `30d` to `14d`) deletes every matching index older than 14 days within minutes
- Rolling back restores the policy, **not** the deleted indices
- So read the dry run's diff line by line, and check which indices use the policy before you apply

## Admins: users and passwords

Admins add people by email, choose their access per cluster, and hand over a generated password as a CSV file.

![Users page with a user selected](images/14-users.png)

The table shows each user's role, their level on each cluster, when they last signed in and their password status:

| Password status | Meaning |
| --- | --- |
| Generated | Still using the password an admin generated |
| Initial (.env) | A bootstrap admin still using `BOOTSTRAP_ADMIN_PASSWORD` from the server's `.env` |
| Set by user | They changed it themselves |
| Locked | 5 failed sign-ins; locked for 15 minutes or until reset |

### Add users

1. Click **Add users**.
2. Enter one or more emails (one per line, or separated by commas).
3. Choose their access on each cluster (**All clusters** applies to every cluster, including ones added later), or tick **Make admin**.
4. Click **Create**. It's all or nothing: if one email already exists, nobody is created and the dialog names who.

![Add users dialog](images/15-add-users.png)

The next screen shows each generated password **once**. Click **Download CSV** (`username,password`) or copy each password, and send each person theirs privately. If you close the dialog without downloading, it warns you first; after that the passwords can't be shown again, so you'd reset them.

![Passwords shown once, with Download CSV](images/16-users-created-csv.png)

### Change access, reset a password, remove a user

Click a user to open the panel on the right.

- **Cluster access / Admin:** change and **Save**. It applies to their next request; no sign-out needed
- **Reset password…:** generates a new password shown once with a CSV, ends all their sessions, and unlocks a locked account
- **Remove user:** asks you to confirm. They can't sign in any more; their past changes stay in the audit log

You can't remove yourself or take away your own admin rights, and bootstrap admins (set in `.env` on the server) can't be changed here. If the only admin is locked out, someone with server access runs `docker compose exec es-config-api python -m app.cli reset-password <email>`.

Treat a downloaded CSV like a key: anyone holding it can sign in as those users. Delete it once each person has their password.

## Admins: the allowlist

The allowlist decides what may be changed at all, for everyone including editors: anything not on it is locked, and a deny pattern always wins over an allow.

![Allowlist editor](images/17-allowlist.png)

There is one card per config type:

| Card | Allow / deny patterns match | Extra |
| --- | --- | --- |
| Cluster settings | setting keys, e.g. `indices.recovery.*` | — |
| Index settings | setting keys, e.g. `index.refresh_interval` | **On indices**: which indices (default: all except system indices) |
| Index mappings | index names, e.g. `delest-log-*` | add-only |
| Index delete | index names, e.g. `my-index` | keep these narrow: deletes are permanent |
| Index templates | template names | **Index patterns**: which `index_patterns` templates may target |
| Component templates, ILM policies, Ingest pipelines | names | — |

- **Unlock** a locked card, type a pattern and press Enter (or **Add**); click × on a pattern to remove it. **Lock** turns a whole type off again
- Patterns use `*` (any characters) and `?` (one character). System indices (starting with a dot) only match patterns that also start with a dot
- **Save allowlist** applies it; **Discard** throws away unsaved edits. **Edit as JSON** shows the same rules as JSON for bulk edits (nothing is saved until you click Save)

**Global vs cluster override.** The **Global** tab applies to every cluster. A cluster's **override** tab replaces the global list for that cluster only: **Create override from global** starts one as a copy, **Remove override** goes back to global.

Every save is recorded in the audit log with the before and after rules.

## Admins: the audit log

The audit log records every change, dry run, rollback, delete, sign-in and admin action, including the ones that were refused, one day at a time in UTC.

![Audit log with a rejected dry run expanded](images/18-audit-log.png)

- **Filters:** date (UTC), cluster, user, action (UPDATE, ROLLBACK, DRY\_RUN, INDEX\_DELETE, ADMIN\_\*, AUTH\_\*) and outcome (Success, Rejected, Failed, No change). The filters stay in the address bar, so you can share a filtered view
- **Expand a row** (click it) to see the error code and message, blocked keys, the reason given, the diff of what changed, the change and request IDs, and the source IP
- **Export JSON** downloads what the filters show
- The page shows up to 500 events; narrow the filters if there are more

The short request ID at the bottom of every red error box in the console is the start of the Request ID here, so a user can tell you exactly which attempt failed. Passwords are never written to the audit log.

## Messages you may see

A red box always means nothing was changed; its last line shows the error code and a short request ID an admin can look up in the audit log.

| Message | Why | What to do |
| --- | --- | --- |
| Not on the allowlist | The key, index or name isn't allowlisted (or is denied) | Ask an admin; admins get an **Open the allowlist** link |
| You don't have access for this | Your level on this cluster is too low | Ask an admin for Edit (or Delete) |
| Changed outside the API (drift) | Someone changed it directly in Elasticsearch | Check the live config; roll back only with the force box if you really want to overwrite their change |
| The config changed since you loaded it | Someone applied a change while you were editing | Reload the page; your edits stay in the editor until you do |
| Another change is in progress | Someone is changing the same thing right now | Wait a few seconds and try again |
| Cluster health is red | Changes are held back while the cluster is red | Fix the cluster first, or tick the force box if the change is the fix |
| This setting can't be changed on an open index | A static index setting | Not possible here; it needs a closed index or a new index |
| Mappings can only gain new fields | You tried to change or remove a field | Add a new field instead, or reindex outside the console |
| Elasticsearch rejected the change | Unknown setting or invalid value | The message has Elasticsearch's reason; fix the JSON |
| Invalid JSON | A typo in the editor (red underline) | Fix the highlighted line |
| Can't reach the cluster | The API server can't connect to Elasticsearch | Tell whoever runs the API server |
| Your session ended | 12 hours passed, or your password was changed or reset | Sign in again |
| Account locked | 5 wrong passwords | Wait 15 minutes or ask an admin to reset your password |

## Good practice and FAQ

**Good practice**

- Read the whole dry-run table before applying, not just the green banner
- Write reasons a colleague will understand in six months: what and why, with a ticket number if there is one
- Change one thing at a time; it keeps rollback simple (there is only one level)
- Keep index-delete and ILM allowlist patterns as narrow as possible
- Don't change configuration directly in Elasticsearch; it causes drift and blocks rollbacks

**FAQ**

- **Can I undo a rollback?** Yes: roll back again. Rollback swaps the live config and the snapshot.
- **Can I go back two changes?** No, only one level. Older snapshots are kept in S3 (bucket versioning) if an admin needs to recover one.
- **Why is a Roll back button greyed out?** Nothing has been changed through the console yet, so there is no snapshot.
- **Why can't I roll back a mapping?** Elasticsearch never removes fields from a mapping.
- **Can I recover a deleted index's documents?** No. Only its settings, mappings and aliases are saved, so an empty copy can be recreated from **Definition**.
- **Who can see my changes?** Admins, in the audit log, with your email, time, reason and diff.
- **Do I need to sign out when an admin changes my access?** No, it applies to your next click.
- **Can scripts do the same things?** Yes, through the API with a token from sign-in; see [API_REFERENCE.md](API_REFERENCE.md).
- **Is there SSO?** Not yet; sign-in is by email and password.
