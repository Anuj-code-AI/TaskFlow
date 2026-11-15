# TaskFlow — Team Workflow Management System

A modular-monolith workflow platform built with FastAPI, PostgreSQL, Redis, and a vanilla HTML/CSS/JavaScript frontend.

## Current implementation status

This repository is a fresh implementation. It provides the initial runnable foundation and core MVP vertical slice. Do not treat unimplemented advanced requirements as complete; see `PROJECT_STATUS.md` and `REQUIREMENTS_CHECKLIST.md`.

## Stack

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic
- PostgreSQL, Redis
- JWT access tokens, Argon2id password hashing, hashed rotating refresh tokens
- Multi-page HTML/CSS/vanilla JavaScript frontend
- Pytest

## Run locally

1. Copy `.env.example` to `.env` and set a strong `JWT_SECRET_KEY`.
2. Run `docker compose up --build`.
3. API: http://localhost:8000
4. Swagger: http://localhost:8000/docs
5. Landing page: http://localhost:8000/
6. Login/register: http://localhost:8000/login and http://localhost:8000/register
7. Workspace application: http://localhost:8000/app

The API container runs Alembic migrations on startup for local development. For production, run migrations as a separate release step.

Optional seed owner/workspace/project (set the `SEED_*` values in `.env`, then run):

```bash
cd backend
python -m app.seed
```

The seed script does not contain a default password and will not run unless a seed email and a password of at least 12 characters are supplied.

## Local development without Docker

```bash
cd backend
python -m venv .venv
# activate the virtual environment
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Set `DATABASE_URL` and `REDIS_URL` in `.env`. The database URL must use the async driver, e.g. `postgresql+asyncpg://postgres:postgres@localhost:5432/taskflow`.

## Tests

```bash
cd backend
pytest -q
```

The API integration tests create an isolated temporary SQLite database. PostgreSQL-specific migration and concurrency behavior must additionally be tested against PostgreSQL; never point tests at production.

## API outline

- `/api/v1/auth`: register, login, refresh, logout, me
- `/api/v1/workspaces`: create/list/get/update
- `/api/v1/workspaces/{workspace_id}/transfer-ownership`: transfer ownership
- `/api/v1/workspaces/{workspace_id}/members`: list/update/remove
- `/api/v1/workspaces/{workspace_id}/invitations`: invite/list/revoke
- `/api/v1/workspaces/invitations/mine`: list invitations for the current user
- `/api/v1/workspaces/invitations/{invitation_id}/accept`: accept an invitation
- `/api/v1/workspaces/{workspace_id}/audit-logs`: workspace/project activity history
- `/api/v1/workspaces/{workspace_id}/projects`: create/list/get/update/archive
- `/api/v1/workspaces/{workspace_id}/tasks`: create/list/get/update/assign/transition/archive
- `/api/v1/workspaces/{workspace_id}/tasks/{task_id}/dependencies`: list/add dependencies; nested DELETE removes a dependency
- `/api/v1/workspaces/{workspace_id}/tasks/{task_id}/comments`: create/list
- `/api/v1/notifications`: list, unread count, mark read, mark all read, stream ticket and SSE stream
- `/health`, `/ready`

## Security notes

- Access tokens are short-lived JWTs.
- Refresh tokens are random opaque values; only their SHA-256 hashes are stored.
- Refresh tokens are rotated on refresh and revoked on logout.
- Refresh token is delivered in an HttpOnly cookie.
- All workspace resources are checked against membership in backend dependencies/services.
- Configure HTTPS, secure cookies, CORS, rate limits, and secrets before public deployment.

## Architecture

See `ARCHITECTURE.md`, `REQUIREMENTS_CHECKLIST.md`, and `PROJECT_STATUS.md`.


## Updated MVP workflow

- Public landing page: `/`
- Registration: `/register`
- Login: `/login`
- Authenticated workspace application: `/app`
- Owners and admins invite users by email; invitations are persisted and pushed to online invitees over Redis Pub/Sub + SSE.
- Invitees can accept pending invitations from the Invitations area after signing in with the invited email.
- Only workspace owners/admins can create projects and create/assign tasks.
- Every new task must be assigned to an active workspace member.
- Assigned members can work on their tasks through `IN_REVIEW`; only owners/admins can mark tasks `DONE`.
- Each project has one owner (the workspace owner), who is also inserted into `project_members`.
- Workspace activity logs can be filtered by project.

### Database migrations

The updated schema adds migrations `0002_invitations_project_owner`, `0003_reliability_dependencies` and `0004_task_labels`. Existing deployments should run:

```bash
alembic upgrade head
```

The Docker API command already runs this migration at startup. Do not remove the PostgreSQL volume to apply it.


## Reliability features added

- Task creation requires `Idempotency-Key`. The key is scoped to the authenticated user and workspace. Replays with the same payload return the original task; reusing a key with a different payload returns HTTP 409.
- Task writes require `expected_version`; stale writes return HTTP 409. Task dependency creation checks for cycles and completion is blocked until all dependencies are DONE.
- Business changes, audit rows and outbox events are committed together. The worker retries failed outbox jobs with exponential backoff and marks jobs DEAD after eight failed attempts. Notification uniqueness prevents duplicate inbox rows.
- Redis caches workspace project lists for 60 seconds. Mutations invalidate the cache; Redis failure falls back to PostgreSQL.
- Login, registration, refresh, invitation and task mutation routes are rate-limited. Redis is preferred; a process-local limiter is used during Redis outages.
- Requests carry an `X-Request-ID` response header. Request payloads are limited to 1 MiB.
- Production mode rejects a weak/default JWT secret and insecure refresh-cookie configuration.

## API examples

Create a task with an idempotency key:

```http
POST /api/v1/workspaces/{workspace_id}/tasks
Authorization: Bearer <access-token>
Idempotency-Key: task-create-2026-001
Content-Type: application/json
```

```json
{
  "project_id": "<project-id>",
  "title": "Implement audit filtering",
  "assignee_id": "<workspace-member-user-id>",
  "priority": "HIGH"
}
```

Archive a task with its current version using `POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/archive` and a JSON body such as `{"expected_version": 3}`.

Update a task with its current version:

```json
{
  "expected_version": 1,
  "title": "Implement audit filtering v2"
}
```

Add a dependency:

```json
{
  "depends_on_task_id": "<blocking-task-id>"
}
```

## Verification

Run unit and API integration tests:

```bash
cd backend
pytest -q
```

The API integration tests use an isolated SQLite database and do not require a running PostgreSQL/Redis service. PostgreSQL migration behavior, Redis live events, Docker image builds and browser flows still need environment-level verification. Use PostgreSQL for deployment and production concurrency behavior.


## Index rationale

- `workspace_members(workspace_id, user_id)` is unique and indexed for tenant authorization and member lookup.
- `tasks(workspace_id, project_id, status)` supports workspace/project board queries; `assignee_id` supports personal task views.
- `task_comments(task_id)` supports chronological task discussion.
- `audit_events(workspace_id, created_at)` supports tenant-scoped audit pagination; `entity_id` supports entity history.
- `notifications(recipient_id, created_at)` supports inbox pagination; `(recipient_id, event_key)` prevents duplicate deliveries.
- `task_dependencies(task_id)` and `task_dependencies(depends_on_task_id)` support dependency traversal in either direction.
- `idempotency_records(scope, key)` is unique to serialize concurrent replays for a caller/workspace scope.
- `outbox_events(status, available_at)` supports due-job polling and retry scheduling.


## AI usage disclosure

ChatGPT was used to assist with requirement analysis, code changes, documentation and test authoring. The submitting developer must review and understand the implementation, verify it against PostgreSQL/Redis, and be able to explain authorization, transaction boundaries, concurrency, idempotency and worker behavior.


## Error response conventions

- `400`: invalid business input or workflow transition.
- `401`: missing/invalid authentication or refresh token.
- `403`: authenticated user lacks permission.
- `404`: resource is absent or outside the caller's workspace.
- `409`: stale task version, dependency completion block, or idempotency conflict.
- `413`: request body exceeds 1 MiB.
- `422`: request validation failure. The response includes field/message/type but never echoes submitted values.
- `429`: rate limit exceeded, with `Retry-After`.
- `500`: generic internal error; SQL, stack traces and configuration are not returned.

Responses include `X-Request-ID` for support and log correlation.


### Workspace and task workflow notes

- Invitations are only created for existing active TaskFlow accounts. Invitees must register before an owner/admin can invite them.
- OWNER/ADMIN can update member roles, remove non-owner members, and manage projects. Only OWNER can transfer workspace ownership.
- Project name and description can be edited from the project board's **Edit project** action.
- New tasks start in TODO. A task starts in BACKLOG only when it is created with a due date already earlier than the current UTC time. The worker also moves unfinished, non-archived tasks to BACKLOG after their due date passes.
- Assigned MEMBER users cannot edit task title, description, priority, due date, labels, or assignee. They can add comments and move their own task one step forward: BACKLOG -> TODO -> IN_PROGRESS -> IN_REVIEW.
- Only OWNER/ADMIN can edit task details, assign tasks, manage dependencies, and mark tasks DONE. OWNER/ADMIN can choose workflow states, but dependency blockers prevent any move into IN_PROGRESS, IN_REVIEW, or DONE until all direct prerequisites are DONE.
- When a task enters IN_REVIEW, the worker creates deduplicated inbox notifications for workspace OWNER and ADMIN users.
- The worker automatically moves overdue, unfinished, non-archived tasks to BACKLOG and records the automated change in audit history.
