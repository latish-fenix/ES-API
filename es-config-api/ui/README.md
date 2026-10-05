# ES Config Console (web UI)

The browser front end for the ES Config API. It lets people read and change Elasticsearch
configuration with a dry run before every change, roll back with one click, and lets admins
manage users, the allowlist and the audit log.

- **End users:** read [`docs/USER_GUIDE.md`](../docs/USER_GUIDE.md) (also shared as a Claude Doc).
- **Scripts / API:** every endpoint with `curl` examples in [`docs/API_REFERENCE.md`](../docs/API_REFERENCE.md).
- **This file:** how the UI is built, run, changed and deployed.

| | |
| --- | --- |
| URL in production | `http://<api-host>/ui/` (for Fenixcommerce: `http://172.0.58.49/ui/`) |
| Stack | React 18, TypeScript 5 (strict), Vite 5, React Router 6, TanStack Query 5, CodeMirror 6 |
| Fonts | IBM Plex Sans / Mono, bundled with `@fontsource` (no internet needed at runtime) |
| Served by | the FastAPI app itself, from `app/static/ui`, same origin as `/api/v1` |
| Browsers | current Chrome, Edge, Firefox, Safari (tested in Chromium) |

---

## Quick start

Prerequisites: Node.js 20+ and a running API (see the main [`README.md`](../README.md) or
`local-test/TESTING.md`), with `AUTH_MODE=password`.

```bash
cd ui
npm ci                 # install exact versions from package-lock.json
npm run dev            # http://localhost:5173/ui/ , proxies /api to http://localhost:8080
```

Point the dev server at another API with `API_URL`:

```bash
API_URL=http://172.0.58.49 npm run dev
```

Sign in with an account that exists on that API (for the local test setup:
`latish.madapada@fenixcommerce.com` with the first password printed by `local-test\2-start-api.cmd`).

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | Dev server with hot reload on :5173, `/api` proxied to `API_URL` (default `http://localhost:8080`) |
| `npm run build` | Type-checks (`tsc -b`), then builds into `../app/static/ui` (emptied first) |
| `npm run typecheck` | Type-check only |

After `npm run build`, the API serves the new build at `/ui/` straight away (restart not
needed; hard-refresh the browser).

## How it is delivered

- **Docker (EC2):** the `Dockerfile`'s first stage runs `npm ci && npm run build`, and the
  second stage copies the result into the image. `docker compose up -d --build` is all you
  need; Node is not required on the host. `app/static/ui` is excluded from the Docker build
  context, so the image always contains a fresh build.
- **Local Windows test:** a built copy is committed in `app/static/ui`, so
  `local-test\2-start-api.cmd` serves the UI without Node. **Rebuild and commit it after
  changing anything in `ui/src`**, or the local run shows the old UI.

The API serves `index.html` for any `/ui/...` path (single-page app routing), caches hashed
files under `/ui/assets/` for a year and `index.html` never, and returns `UI_NOT_BUILT` if the
folder is missing.

## Project structure

```
ui/
├── index.html              entry HTML (Vite)
├── public/favicon.svg
├── vite.config.ts          base /ui/, outDir ../app/static/ui, dev proxy, vendor chunks
├── src/
│   ├── main.tsx            fonts, styles, QueryClient, router (basename /ui), toasts
│   ├── App.tsx             all routes
│   ├── api.ts              fetch wrapper, ApiError, response types
│   ├── session.tsx         current user, clusters, health, RequireAuth / RequireAdmin
│   ├── format.ts           number/date/byte formatting, labels, starter JSON for "New"
│   ├── glob.ts             allowlist pattern matching (mirrors the API's fnmatch rules)
│   ├── styles.css          design tokens (light + dark) and every component style
│   ├── components/
│   │   ├── Shell.tsx       sidebar, cluster switcher, top bar, page frame
│   │   ├── ChangeFlow.tsx  edit → dry run → apply; rollback + snapshot dialogs; status badges
│   │   ├── DiffTable.tsx   added / changed / removed table
│   │   ├── JsonEditor.tsx  CodeMirror JSON editor with linting and optional autocomplete, themed by CSS variables
│   │   ├── ui.tsx          Callout, Badge, Dialog (focus trap), toasts, ErrorCallout, helpers
│   │   └── icons.tsx       stroke icon set
│   └── pages/
│       ├── Login.tsx, ChangePassword.tsx
│       ├── Overview.tsx, ClusterSettings.tsx
│       ├── Indices.tsx (+ delete dialog), IndexDetail.tsx (settings / mapping tabs)
│       ├── Data.tsx             data browser: query, filters, time range, columns, sort, document view, export
│       ├── DataEdit.tsx         document edit / create / delete / history + bulk update / delete dialogs
│       ├── NamedResources.tsx   index/component templates, ILM policies, ingest pipelines
│       ├── Requests.tsx         approval requests: list, one request, approve / reject, ChangePreview
│       ├── ShellPage.tsx        the Shell: request line with suggestions, body with autocomplete, response, history
│       ├── CreateIndex.tsx, Rollback.tsx   create-index dialog; roll back any change
│       └── admin/Clusters.tsx, admin/Users.tsx, admin/Allowlist.tsx, admin/Audit.tsx
```

### Routes

| Path | Screen | Who |
| --- | --- | --- |
| `/login` | Sign in | everyone |
| `/` | redirects to the last-used (or first) cluster | signed in |
| `/c/:cluster` | Overview | view+ |
| `/c/:cluster/cluster-settings` | Cluster settings (listed under Administration) | admin |
| `/c/:cluster/indices` (`?tab=deleted`) | Indices / deleted indices | view+ |
| `/c/:cluster/indices/:index/settings` · `/mapping` | Index detail | view+ |
| `/c/:cluster/data?index=&q=&f=&tf=&tg=&tl=&sort=&from=&size=&cols=` | Data browser (all search state in the URL, so a search can be bookmarked or shared); edits need edit on the document's index | view+ |
| `/c/:cluster/{index-templates,component-templates,ilm-policies,ingest-pipelines}[/:name]` (`?new=1`) | Named resources | view+ |
| `/c/:cluster/shell` | Shell (Dev Tools-style requests; writes go through the change flows) | view+ |
| `/requests` (`?tab=pending\|all`, `status`, `clusterId`, `requestedBy`) · `/requests/:id` | Approval requests: your own, or (admins) the queue and every request | signed in |
| `/account/password` | Change password | signed in |
| `/admin/clusters` · `/admin/users` · `/admin/allowlist` · `/admin/audit` | Administration | admin |

## How it works

### Authentication and security

- `POST /api/v1/auth/login` sets an `HttpOnly`, `SameSite=Strict` cookie (`esc_session`). The
  UI never reads or stores the token; every request uses `credentials: "same-origin"`.
- Every request sends `X-Requested-With: es-config-ui`; the API refuses cookie-authenticated
  writes without it (CSRF protection).
- `GET /api/v1/me` decides what is shown (admin menu, edit panels, delete buttons). The API
  enforces the same rules again, so hiding a button is convenience, not security.
- A 401 `NOT_AUTHENTICATED` / `SESSION_EXPIRED` from any call clears cached data and sends the
  user to `/login?next=<page>`; `next` only accepts same-app paths (no open redirect).
- Generated passwords exist only in component state while the dialog is open. "Download CSV"
  builds the `username,password` file in the browser; nothing is cached or logged.
- Responses under `/ui` carry `Content-Security-Policy` (`script-src 'self'`, no external
  hosts; `style-src 'unsafe-inline'` only because CodeMirror injects styles),
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.
- React escapes all rendered text; the code has no `dangerouslySetInnerHTML`.
- The clipboard API needs HTTPS; over plain HTTP `copyText` falls back to a hidden textarea.

### Data fetching

TanStack Query caches every GET under a key such as `["config", path]`, `["indices", cluster]`
or `["admin-users"]`. Writes are `useMutation`s that invalidate the affected keys on success.
Client errors (4xx) are never retried; network errors and 5xx are retried twice. Cluster
health refreshes every 60 s.

**Permissions in the UI.** `/me` returns `access` per cluster (`{default, indices: [{pattern, level}]}`).
`useCluster()` in `session.tsx` gives `level` (cluster default), `can(level)`, `canIndex(index, level)`
(same most-specific-pattern rule as the API, via `glob.ts`) and `hasAccess`. The UI only uses
them to hide buttons; the API enforces everything.

The data browser (`pages/Data.tsx`) keeps its search in the URL and runs it with TanStack Query
(`["data-search", cluster, index, body]`, previous page kept while the next loads). Chosen
columns are remembered per cluster + index in `localStorage` (wrapped in try/catch); the
default is the first document's top-level fields. Export uses `downloadPost` in `api.ts`
(a fetch that returns a Blob) because the response is a file, not JSON.

Document writes (`pages/DataEdit.tsx`) always preview first: `PUT …/_doc/{id}?dryRun=true` for
the diff, then the real call with `ifSeqNo`/`ifPrimaryTerm` from the search hit. Bulk dialogs
keep the dry run's `dryRunToken` and send it back with `expectedCount` (which the user types).
On success they invalidate `data-search`, `data-fields`, `indices`, `doc-history` and
`bulk-changes`.

The Overview's node table polls `GET /clusters/{id}/nodes` every 30 s (`["nodes", cluster]`).

Adding, editing or removing a cluster (`pages/admin/Clusters.tsx`) invalidates
`admin-clusters`, `clusters`, `me` and `admin-users`, so the cluster switcher and the Users
page update at once. Passwords and API keys are only ever sent, never received: the edit form
leaves them empty and the API keeps the saved value.

### The change flow (`ChangeFlow.tsx`)

Every config screen reuses one component:

1. The JSON editor holds the draft (`text` state lives in the page, so tables can insert keys).
2. **Dry run** → `PUT <path>?dryRun=true` → `DryRunSummary` renders the diff, warnings,
   `noChange`, `valid:false` errors and Elasticsearch simulations (template resolution,
   pipeline sample docs).
3. Editing the text after a dry run cancels it (stage goes back to "edit").
4. **Apply** → `PUT <path>` with `reason` and `If-Match: "<version>"` from the GET, so a
   concurrent change returns 412 `VERSION_MISMATCH` instead of being overwritten.
5. `RollbackDialog` loads `…/previous` and a rollback dry run for the preview; a
   `DRIFT_DETECTED` answer offers a force checkbox. `CLUSTER_UNHEALTHY` offers force on apply.

Props of note: `permanent` (mappings: adds the "I understand" tick), `sampleDocs` (pipelines),
`toConfig` (reshape before sending), `absent` (the word shown for a missing value: "default"
for settings, "—" for resources).

### Approvals

A non-admin's real write answers `202 {"pendingApproval": true, "approval": …}`. `request()` in
`api.ts` turns that into a thrown `PendingApproval` (a subclass of `ApiError`), so no dialog
runs its "applied" path, and calls the handler `Shell.tsx` registers: a toast and a refresh of
the `["approvals"]` queries (the sidebar count polls `/approvals/_count` every minute).
`ErrorCallout` shows a `PendingApproval` as a blue "Sent to the admins for approval" notice
with a link to the request, and the dialogs disable their button after it. `useNeedsApproval()`
(from `/me`'s `approvalsRequired`) and `applyLabel()` turn **Apply** into **Request approval**.
`pages/Requests.tsx` holds the list, the request page and `ChangePreview` (the stored dry run
shown per kind of change), which the Shell reuses.

### Shell (`pages/ShellPage.tsx`)

Method + path (`PathInput`: suggestions for the segment being typed from the index list and
known endpoints; a pasted `GET my-index/_search` line is split), a CodeMirror JSON body with an
`autocompletion` source of the target's fields (`…/data/<target>/_fields`) and DSL words, and a
read-only response editor. Non-GET requests are sent with `dryRun: true` first; a `kind:
"write"` answer shows `ChangePreview` and asks for the reason / typed confirmation / count
before the real call. History comes from `…/shell/history`; **Copy as curl** builds the API
call.

### Errors

`ApiError` carries `status`, `code`, `message`, `details` and the response's `X-Request-ID`.
`ErrorCallout` maps known codes (`NOT_ALLOWLISTED`, `DRIFT_DETECTED`, `VERSION_MISMATCH`, …)
to a plain title and a hint, and always prints the code and a short request ID that an admin
can find in the audit log.

### Styling

- One stylesheet, `src/styles.css`. Colours, borders and shadows are CSS variables on `:root`,
  redefined for dark mode under `prefers-color-scheme: dark` (force light with
  `<html data-theme="light">`). Components use tokens only, never raw colours.
- Layout breakpoints: two-column grids stack under 1100 px; under 800 px the sidebar becomes
  an off-canvas menu and wide tables scroll horizontally.
- The visual design follows the approved design canvas (IBM Plex, slate sidebar `#0f172a`,
  accent `#1d4ed8`).

### Accessibility

Labelled form controls, visible focus rings, `role="dialog"` + `aria-modal` with a focus trap
and Escape to close, `aria-live` toasts, `aria-current` on the active nav item and breadcrumb,
text alternatives on icon-only buttons, and no colour-only status (badges carry words).

## Making changes

**Add a screen**

1. Create `src/pages/MyScreen.tsx`; wrap it in `<Page crumbs={…} title="…">` from `Shell`.
2. Add a `<Route>` in `App.tsx` (inside the `RequireAuth` shell; wrap in `RequireAdmin` if needed).
3. Add a `NavItem` in `Shell.tsx`.
4. Read data with `useQuery({ queryKey, queryFn: () => get<T>("/path") })`; write with
   `useMutation` + `request()`, then `queryClient.invalidateQueries`.
5. Add response types to `api.ts`.

**Add a config type:** add it to `NAMED_TYPES` in `api.ts` and to `NAMED_META` /
`NEW_TEMPLATES` in `format.ts`; `NamedResources` and the router pick it up. The API must
support it first.

**Conventions:** TypeScript `strict` (no unused locals/params), no new runtime dependencies
without a reason, tokens instead of colours, and user-facing text in plain words (the error
code goes in the small print, not the headline).

## Testing

- `npm run build` must pass (it type-checks).
- Backend tests cover how the UI is served: `pytest -q tests/test_auth.py -k "security_headers or auth_config"` checks the
  SPA fallback, cache headers and security headers.
- Manual end-to-end check: the 23-step tour in `local-test/TESTING.md` §1a (about 10 minutes)
  covers sign-in, dry run/apply/rollback, mappings, ILM, pipelines, users + CSV, allowlist,
  audit, delete, adding / editing / removing a cluster, the data browser, document edits with undo, bulk changes, index rules and node stats.
- The UI was verified end to end with Playwright (Chromium) against Elasticsearch 8.17.1,
  including a view-only user, dark mode, 1280 px and phone widths. Those scripts are not in
  the repo yet.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `/ui/` returns `UI_NOT_BUILT` | Run `npm run build` (or rebuild the Docker image) |
| Old UI after a change | Rebuild; hard-refresh (Ctrl+F5). `index.html` is `no-cache`, assets are hashed |
| Dev server: every call 502/ECONNREFUSED | The API isn't on `API_URL` (default :8080) |
| Sign-in says "Password sign-in is off" | The API runs with `AUTH_MODE=header`; the UI needs `AUTH_MODE=password` |
| Writes fail with `CSRF_CHECK_FAILED` | Something is calling the API with the cookie but without `X-Requested-With`; use `request()` from `api.ts` |
| `npm ci` fails on EC2/Docker | The build host needs access to registry.npmjs.org (and Docker Hub for `node:20-alpine`) |
