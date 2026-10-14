# CaviNet Backend

REST API and background analysis worker of [CaviNet](https://github.com/ahmadraza225/CaviNet_V1):
AI decision support for differentiating pulmonary TB from NTM lung disease on chest CT.

This repository is included in the main repository as the `backend/` folder (a git submodule).
Run the whole system from there with `make up`.

| Path | Contents |
|---|---|
| `app/api/` | Routes (`routes/`: auth, admin, patients, cases, dashboard, notifications, model, health) and dependencies |
| `app/core/` | Settings, database, Redis, security (hashing, JWT), logging |
| `app/models/` | SQLAlchemy 2 models |
| `app/schemas/` | Pydantic request and response models |
| `app/services/` | Business logic used by the routes and the worker |
| `app/workers/` | RQ queue, jobs and the analysis pipeline |
| `alembic/versions/` | Database migrations, one per schema change |
| `tests/` | pytest: unit and API tests, integration tests on PostgreSQL and Redis |
| `Dockerfile` | API and worker image, built by `docker compose` from the main repository |

**Stack:** Python 3.11, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis 7 + RQ,
ReportLab. The AI pipeline comes from [CaviNet-ML](https://github.com/ahmadraza225/CaviNet-ML).

## Development

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.8,<3"
pip install ../ml -e ".[dev]"     # inside the main repository; or: pip install git+https://github.com/ahmadraza225/CaviNet-ML.git
pytest                            # unit and API tests
ruff check . && ruff format --check .
```

Integration tests run against real services when `TEST_DATABASE_URL` and `TEST_REDIS_URL` are
set. API documentation is served at `/api/docs` while the stack is running.
