# CLAUDE.md

Guidance for Claude Code and other AI assistants working in this repository.

## What this project is

Human Resilience Map is an extreme-heat planning application. It scores US census tracts for heat
vulnerability, shows cooling-center access, and lets planners ask questions and test siting
scenarios. Language models interpret questions and explain results; deterministic code computes
every number. See `README.md` for the architecture and data flows.

## Commands

```bash
make install     # install dependencies (uv)
make db-up       # start local PostGIS
make migrate     # apply database migrations
make up          # full stack in containers (UI on http://localhost:8050)
make lint        # ruff check, ruff format --check, mypy
make fmt         # format and auto-fix
make test        # unit tests (no database)
make test-all    # unit + integration (needs `make db-up`)
```

Before finishing a change, run `make lint` and `make test`. CI runs the same checks.

## Layout

All code is under `src/resilience_map/`:

| Path | Responsibility |
| --- | --- |
| `frontend/` | Dash UI, login and session handling |
| `backend/` | FastAPI routes (data, assistant, imports, jobs) |
| `agents/` | Typed plans, bounded tools, LangGraph workflow |
| `risk/`, `gis/`, `optimization/`, `verification/` | Deterministic scoring, geometry, solver and checks |
| `ingestion/` | Source acquisition, validation, immutable publication |
| `repository/`, `db/` | SQL access and migrations |
| `prompts/` | Versioned prompts, one folder per role and version |
| `workers/` | Queue consumer and outbox dispatcher |

## Hard rules

1. **Never invent data.** No made-up facilities, capacities, costs, coordinates or statistics.
   Missing inputs stay visible. Fictional fixtures belong only in `tests/`.
2. **Models interpret and explain; code computes.** Do not let a model produce scores, routes or
   optimization results. Generated text is checked against tool output.
3. **Versions are immutable.** Published datasets are never edited in place; a change publishes a
   new version. Prompts are versioned: add a new version folder instead of editing a released one.
4. **Secrets never go in Git, logs, chat or Compose files.** Local secrets live in the gitignored
   `.env`. Staging and production read from AWS SSM (`HRM_SECRET_SOURCE=ssm`). Never print a
   secret value.
5. **Keep the safety limits.** 500 tracts per analysis or question, a 100 km and 2,500 km2
   planning box, and the solver and token budgets protect memory and model spend. Do not raise
   them without a stated reason.
6. **Keep routes authenticated.** Every API route depends on `current_user`. The only public
   paths are health, login, callback, logout and static assets.
7. **Ask before production actions.** Deploying, spending money, changing DNS, IAM or secrets,
   and deleting data need explicit approval. Never run `docker compose down -v` on a live stack.

## Code conventions

- Python 3.11 to 3.13, `src` layout, type hints throughout (`mypy src` must pass).
- Ruff with line length 100 (rules E, F, I, B, UP, S, C4, SIM, RUF). SQL is parameterized.
- Unit tests in `tests/unit` need no database; integration tests in `tests/integration` need
  PostGIS. Add a regression test with every bug fix.
- Adding an input to a Dash callback means updating the callback tests in
  `tests/unit/test_frontend.py`.
- Keep `README.md` accurate. If a change alters what is deployed or how to run it, update it.

## Verifying changes

Check UI changes in a real browser, not only with `curl`. Test the first request after a restart
as well as warm requests.

## Git

- Small commits with a clear message that says why.
- Never commit `.env`, `runtime.env`, `database.env`, `aws.env`, Terraform state or plan files,
  or private keys. They are gitignored; run `git status` before committing.
- Do not force-push `main`. Push only when asked.
