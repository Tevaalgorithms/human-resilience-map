# Human Resilience Map

A planning application for exploring extreme-heat vulnerability and cooling-center access.
The Dash map and FastAPI backend combine versioned regional datasets, deterministic GIS and
optimization tools, and five configurable LLM roles coordinated by LangGraph.

This is a planning pilot. Scores are historical planning indices, not medical predictions.
Recommendations depend on reviewed facility inventories, candidate suitability, capacities,
costs and travel assumptions. Missing inputs stay visible rather than being invented.

## What you can do

- Select a study area, county and optional map bounds; explore tract scores and indicators.
- Ask questions in **Ask about this area**, with answers grounded in the selected dataset.
- Run asynchronous rankings, tract explanations and cooling-site analyses; compare scenarios
  and inspect bounded sensitivity results, verification and provenance.
- Upload cooling-center CSVs or complete region ZIPs, review validation, then publish an
  immutable dataset version. Download published source packages for editing or reuse.
- Download selected-region Census/ACS data and, for Los Angeles County, an official cooling
  inventory. Raster enrichment, reviewed candidate sites and network roads need separate preparation.
- Review current monitoring feeds separately from the historical scoring dataset.

## Local setup

For the full container stack, install Docker with Compose. For native development and source
preparation, also install [uv](https://docs.astral.sh/uv/) and Python 3.11–3.13.

1. Create your private configuration **only if `.env` does not already exist**:

   ```bash
   cp -n .env.example .env
   chmod 600 .env
   ```

2. Edit `.env` locally. Set `HRM_DB_PASSWORD`, `HRM_SESSION_SECRET` and `OPENAI_API_KEY`.
   Keep `HRM_SECRET_SOURCE=environment` and `HRM_LLM_PROVIDER=openai` for local AI testing.
   Generate a new database password and session secret separately with:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   If the database volume already exists, use its current database password. Changing
   `POSTGRES_PASSWORD` in Compose does not rotate an existing PostgreSQL user's password.
   Choose the model through `HRM_OPENAI_MODEL`; all five model roles share that configuration.
   The explicit `HRM_LLM_PROVIDER=stub` option runs offline rules/templates without real AI.

3. Start the application:

   ```bash
   docker compose --profile app up -d --build
   ```

   Compose starts PostGIS, applies migrations, then runs the API, UI, dispatcher and worker.
   Open **http://localhost:8050**. API documentation is at **http://localhost:8000/docs**.
   **Check model connection** performs a small live API call and consumes tokens; model status
   alone only checks configuration. Missing credentials and provider failures are reported explicitly.

4. Publish a regional dataset using the upload panel or the preparation workflow below.
   A fresh checkout has no bundled production datasets and does not seed fictional records.

Stop the stack with `docker compose --profile app down`. Named volumes retain database and
uploaded-source data. Provider options are set in `.env` (see `.env.example`).
Local authentication trusts a development identity; cloud deployments require Cognito.

### Native development

```bash
make install
make db-up
make migrate
```

Run `make api`, `make frontend`, `make dispatcher` and `make worker` in separate terminals.
The dispatcher and worker are required for asynchronous analyses. Native services load the
project's private `.env`; its database host should be `localhost`.

## Regional data and uploads

Open **Upload region data or cooling centers** beside the map:

- **Cooling centers CSV:** select an existing study area, download the template, fill it from
  reviewed local evidence, then validate and publish. This replaces the entire study area's
  inventory; a nonempty CSV is marked partial. Blank capacity remains unknown.
- **Complete region source ZIP:** enter a region ID and display name. Put `manifest.json`,
  `tracts.geojson`, `indicators.csv`, `centers.csv` and `candidates.csv` at the ZIP root, with
  optional `roads.json`. Review **Validate upload** before **Publish validated dataset**.
- **Download selected dataset sources:** export the original published sources as a ZIP.

Source datasets under `data/` and runtime files under `.data/` are local and ignored by Git.
If your workspace contains `data/samples/travis-region.zip`, it can be uploaded through
**Complete region source ZIP**. It is a local real-data example, not bundled with a fresh
checkout, and its missing cooling inventory limits site-planning conclusions.
Synthetic data exists only under `tests/fixtures` for offline automated tests.

For online preparation, set `HRM_CENSUS_API_KEY` and `HRM_ENABLE_PREPARATION=true` in the private
`.env`, then recreate the API service. Select map bounds and enter the state/county FIPS in the
UI's download controls. **Save source ZIP** exports the completed preparation. Census-only
packages are incomplete for publication until required heat/canopy indicators are added.
The online cooling option supports LA County (`06/037`); other regions need local inventory uploads.
An empty provider response does not establish that no cooling centers exist.

For off-host preparation:

```bash
uv sync --extra dev --extra geo
uv run --extra geo hrm prep-census \
  --state 48 --counties 453 --year 2023 \
  --bbox='-97.85,30.20,-97.65,30.40' --out data/region-work
uv run --extra geo hrm prep-enrich --dir data/region-work --years 2021,2022,2023
```

Fill inventories from reviewed evidence, add buffered pinned roads for network routing, and
validate the sources. Region preparation covers road preparation, California transit, county packages and ZIP
upload requirements; inventories carry status, hours, capacity, cost and confirmation metadata.
To publish a complete validated local package into the native development database:

```bash
uv run hrm validate-import --dir data/region-work
HRM_INGEST_DIR=data/region-work \
HRM_INGEST_STUDY_AREA_ID=your-region \
HRM_INGEST_STUDY_AREA_NAME='Your reviewed region' \
uv run hrm ingest --source files
```

Native ingestion uses `.env` database/object-store settings. Container services use their own
shared object-store volume; use the UI/API upload path when working with the container stack.

## Agents and prompts

Five LLM roles use the configured OpenAI or Bedrock model. Their versioned prompts are in
[`src/resilience_map/prompts/`](src/resilience_map/prompts/), with `system.md` and `user.md` in each version.

| Role | Active prompt | Responsibility |
| --- | --- | --- |
| Request interpreter | `interpret_request/v2` | Convert analysis questions into typed requests |
| Result explainer | `explain_results/v2` | Explain computed evidence and verified results |
| Area question planner | `area_question_planner/v1` | Route area questions to approved read-only tools |
| Data quality advisor | `data_quality_advisor/v1` | Explain missing inputs and acquisition steps |
| Scenario advisor | `scenario_advisor/v1` | Suggest planning experiments and explain readiness limits |

Readiness, scoring, routing, optimization, comparison, sensitivity and verification use
deterministic code. Scenario advice does not execute a site analysis; use **Run analysis**.
Answers carry model/prompt metadata and evidence. Numeric and identifier grounding checks can
replace unsupported generated text with a deterministic summary; they do not verify every prose claim.
Historical prompt versions are retained for reproducibility.

## Architecture and API

| Component | Location | Purpose |
| --- | --- | --- |
| UI | `src/resilience_map/frontend/` | Dash map, area questions, uploads and analysis results |
| API | `src/resilience_map/backend/` | Authenticated data, assistant, import and job endpoints |
| Agent workflow | `src/resilience_map/agents/` | Typed plans, bounded tools and LangGraph orchestration |
| Analytical tools | `risk/`, `gis/`, `optimization/`, `verification/` | Reproducible calculations and checks |
| Data ingestion | `src/resilience_map/ingestion/` | Acquisition, source validation and immutable publication |
| Persistence | `repository/`, `db/`, `workers/` | PostGIS, migrations, queue/outbox and checkpointed jobs |
| Infrastructure | `infrastructure/`, `deploy/` | Terraform and deployment utilities |

The API persists jobs and an outbox; the dispatcher queues work and the worker executes the
checkpointed workflow. Results keep dataset/scoring versions, tool evidence and verification.
Cognito provides cloud authentication. Queue/object-store adapters support local development
and AWS deployments. See [architecture](docs/architecture.md).

Useful authenticated endpoints include:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/assistant/questions` | Ask about a pinned selected area |
| `POST /v1/analyses` | Submit an asynchronous analysis |
| `GET /v1/model`, `POST /v1/model/check` | Model configuration status and live connection check |
| `GET /v1/cooling-centers/template` | Download a blank cooling inventory CSV |
| `POST /v1/imports`, `POST /v1/imports/{id}/publish` | Stage/validate sources, then publish |
| `GET /v1/datasets/{version}/download` | Export a published source ZIP |
| `POST /v1/preparations` | Start bounded online regional preparation |

Use the running API's `/docs` for schemas and all routes. Local API requests use `X-Dev-User`;
cloud requests require the configured Cognito authentication.

## Secrets and production

Local secrets belong only in the ignored `.env`. `.env.example` contains blank secret fields;
`.env.production.example` contains non-secret selectors. Secret files, Terraform state/plan files
and private keys are excluded from Git and Docker builds.

Staging/production require `HRM_SECRET_SOURCE=ssm` and Cognito. The application reads required
SecureString values from AWS SSM Parameter Store without falling back to `.env` credentials.
The standalone Terraform in `infrastructure/ssm` creates five placeholder parameters and optional
reader policies; **you apply it and enter the actual values manually in AWS**. No API key is
provided to Terraform, and creating a placeholder does not make the application ready.
Database/Cognito values must match the underlying credentials. Runtime AWS permissions and
protected Terraform state are required.

Deployment options and operational prerequisites are documented in [deployment](docs/deployment.md). Inventory review, model evaluation, security review, load testing
and backup/restore rehearsal remain required before operational use.

## Checks and repository cleanup

```bash
make lint        # Ruff, formatting and mypy
make test        # offline/provider-mocked unit tests
make test-all    # also integration tests against local PostGIS
```

Integration tests recreate a dedicated test database. Set `HRM_TEST_DATABASE_URL` to a disposable
PostGIS database URL matching your local credentials; never point it at an application database.
Tests use stub/mocked providers and do not require paid model calls or AWS account access.

```bash
python3 scripts/clean_generated.py --dry-run
make clean
# Optional: also remove downloaded Terraform providers/modules (init recreates them):
python3 scripts/clean_generated.py --include-terraform --dry-run
python3 scripts/clean_generated.py --include-terraform
```

Cleanup preserves source files, migrations, prompt history, `.env`, `.venv`, datasets, runtime data,
Terraform state and lockfiles. Run it after verification because checks recreate caches.
Before the first GitHub upload, review the staged file list and confirm that no private files
are included. Keep provider lockfiles and `uv.lock` for reproducible installs.

Architecture decisions: [ADR-001](docs/ADR-001.md) and [ADR-002](docs/ADR-002-terraform.md).
