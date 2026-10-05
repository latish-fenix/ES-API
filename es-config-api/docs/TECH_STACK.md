# ES Config API — Languages and technology

*5 October 2026. The same page, kept in sync, is also a shared Claude Doc.*

The project uses two main languages: **Python** for the API and **TypeScript** (React) for the web console. The rest is CSS for styling, PowerShell for the Windows test scripts, and YAML, JSON and a Dockerfile for configuration.

## Summary

| Part | Language | Framework |
| --- | --- | --- |
| API (backend) | Python 3.12 | FastAPI |
| Web console (frontend) | TypeScript 5 | React 18, built with Vite |
| Data, secrets, email | (services) | Elasticsearch 8.17, AWS S3, AWS Secrets Manager, Amazon SES |
| Deployment | Dockerfile, YAML | Docker Compose on EC2 |

Size (lines of code, generated files and docs not counted): 18,600 lines, 95% Python and TypeScript.

| Part | Language | Lines |
| --- | --- | --- |
| Web console | TypeScript | 7,019 |
| API | Python | 7,879 |
| Tests | Python | 2,604 |
| Console styles | CSS | 483 |
| Local test scripts | PowerShell | 252 |
| Server scripts | Python | 148 |
| Config and policy | YAML / JSON | 170 |
| Container | Dockerfile | 45 |

## Backend: Python

The API is **Python 3.12** (the Docker image; 3.10 or newer works locally) on **FastAPI**, in `es-config-api/app/`.

| Library | Version | Used for |
| --- | --- | --- |
| FastAPI | 0.115+ | HTTP API, request validation, the OpenAPI page at `/docs` |
| Uvicorn | 0.32+ | The web server (2 worker processes in Docker) |
| Pydantic | 2.x | Request and response models, validation |
| elasticsearch-py | 8.17 | Talking to the Elasticsearch clusters |
| boto3 | 1.35+ | AWS: S3 (state, snapshots, audit, document versions, approval requests), Secrets Manager (every password, key and hash) and SES v2 (approval emails) |
| PyYAML | 6.0+ | `config/clusters.yaml` and `data/clusters.managed.yaml` |
| Python standard library | | scrypt password hashing (`hashlib`), signed session tokens (`hmac`), locking, JSON |

Main modules: `routes_*.py` (the endpoints), `service.py` (dry run, apply, rollback), `handlers/` (one per config type), `data_browser.py` and `data_edit.py` (documents), `identity.py` (access levels and index rules), `secret_store.py` (Secrets Manager), `storage.py` (S3), `nodes.py` (node stats), `cli.py` (server commands such as `reset-password` and `migrate-secrets`), `approvals.py` and `mailer.py` (approval requests, SES email), `shell.py` (the Shell: which requests are allowed and how writes map onto the change flows).

## Web console: TypeScript and React

The console is **TypeScript 5** (strict mode) with **React 18**, built by **Vite 5** into `app/static/ui/` and served by the API at `/ui/` (same origin, so no CORS). Source is in `es-config-api/ui/src/`; styling is plain **CSS** (`styles.css`), no CSS framework.

| Library | Version | Used for |
| --- | --- | --- |
| React, React DOM | 18.3 | The screens |
| React Router | 6.28 | Pages and addresses (`/ui/c/<cluster>/...`) |
| TanStack Query | 5.62 | Loading and caching API data, refreshes |
| CodeMirror 6 (`@uiw/react-codemirror`, `@codemirror/autocomplete`) | 4.23 / 6.20 | The JSON editors with syntax checks; field and query-word suggestions in the Shell |
| IBM Plex Sans / Mono (`@fontsource`) | 5.3 | Fonts, bundled (nothing loaded from the internet) |
| Vite + `@vitejs/plugin-react` | 5.4 | Dev server and production build |

Building needs **Node.js 20** (the Dockerfile's first stage does it; locally `cd ui && npm run build`).

## Platform and services

| Piece | What it is here |
| --- | --- |
| **Elasticsearch 8.17** (self-managed) | The clusters the console configures and browses, reached over their REST API with a dedicated service account (`config_api_writer` role) |
| **AWS S3** (us-west-2) | The only data store: users (without passwords), allowlist, snapshots for rollback, audit log, deleted-index definitions, document versions and bulk backups, approval requests, shell history. Conditional writes instead of a database |
| **AWS Secrets Manager** (us-west-2; older secrets read from us-east-1 until moved) | Every secret: session signing key, first-admin password, each user's password hash, each cluster's password or API key |
| **Amazon SES** (us-west-2) | Approval emails to the admins and the requester, from `fenix_int_product_alerts@fenixcommerce.com` |
| **AWS IAM** (EC2 instance role) | How the API gets AWS access; no AWS keys on the server |
| **Docker + Docker Compose** | One container on the EC2 server: a Node 20 stage builds the console, then a `python:3.12-slim` image runs the API as a non-root user |
| **Amazon EC2** | Hosts the container (`172.0.58.49`, port 80) |

There is no database server, message queue or cache service.

## Tests and tooling

| Tool | Used for |
| --- | --- |
| **pytest** 8 | 86 tests in `tests/`, run against a real Elasticsearch 8.17 |
| **moto** 5 | Simulates S3, Secrets Manager (both regions) and SES in the tests |
| **httpx** / FastAPI `TestClient`, **requests** | Calling the API in tests and in `scripts/smoke_test.py` |
| **TypeScript compiler** (`tsc -b`) | Type-checks the console on every build |
| **Playwright** (Chromium) | Browser checks of the console and the screenshots in the User Guide (run during development, not in the repo) |
| **Git + GitHub** | Source control (`latish-fenix/ES-API`) |

## Other file types in the repo

| Type | Where | What for |
| --- | --- | --- |
| YAML | `config/clusters.example.yaml`, `docker-compose.yml`, `data/clusters.managed.yaml` (server only) | Cluster definitions, the container setup |
| JSON | `docs/iam-policy.json`, `ui/package.json`, `ui/tsconfig.json` | AWS policy, console dependencies and compiler settings |
| Dockerfile | `Dockerfile` | The two-stage image build |
| `.env` (key=value) | `.env.example` → `.env` on the server | Settings; no passwords |
| PowerShell, `.cmd` | `local-test/` | One-click local test on Windows (installs Elasticsearch, starts the API) |
| Markdown | `README.md`, `docs/`, `ui/README.md`, `local-test/TESTING.md` | The guides, kept in sync with the shared Claude Docs |
| HTTP / `curl` | examples in the docs | Elasticsearch Dev Tools calls and API calls |
