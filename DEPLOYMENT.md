# Deployment Guide (Not Yet Deployed)

This repository is prepared for a split API/worker deployment. Actual deployment requires a public Git repository and hosting credentials.

## Required services

- One hosted PostgreSQL database.
- One hosted Redis instance.
- One web/API service.
- One background worker service using the same image and environment.

## API service

Configure the host to build from the repository root using `backend/Dockerfile`.

Start command:

```bash
sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT"
```

If the platform supports release/pre-deploy commands, run `alembic upgrade head` there and use only the Uvicorn command as the web start command.

## Worker service

Create a separate background worker using the same Docker image and working directory. Start command:

```bash
python -m app.workers.main
```

The worker must use the same PostgreSQL and Redis URLs as the API. Do not run migrations from the worker.

## Required environment variables

| Variable | Production value |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Hosted PostgreSQL URL using `postgresql+asyncpg://` |
| `REDIS_URL` | Hosted Redis URL |
| `JWT_SECRET_KEY` | Random secret, at least 32 characters |
| `COOKIE_SECURE` | `true` |
| `COOKIE_SAMESITE` | `lax` or `strict` |
| `CORS_ORIGINS` | Exact frontend/API origin(s), comma-separated |
| `ACCESS_TOKEN_TTL_MINUTES` | `15` or a reviewed value |
| `REFRESH_TOKEN_TTL_DAYS` | `14` or a reviewed value |

Never commit production environment files, database credentials, Redis credentials or JWT secrets.

## Verification after deployment

- `GET /health` — process liveness.
- `GET /ready` — PostgreSQL and Redis readiness; returns 503 if either dependency is unavailable.
- `/docs` — deployed Swagger UI.
- `/openapi.json` — OpenAPI schema.
- Confirm the worker is running separately and outbox rows are being processed.
- Run a smoke workflow: register, create workspace, invite a member, create project/task, add comment, transition task, inspect audit history and notification inbox.
- Confirm refresh-token rotation and logout.
- Confirm stale task versions return 409.
- Confirm task creation replay returns the original task and a mismatched payload returns 409.

## Deployment status

No service has been deployed from this editing session. Public API, readiness, Swagger and repository URLs must be filled in after deployment and verified from outside the hosting environment.
