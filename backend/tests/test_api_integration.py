import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.main import app
from app.core.database import get_db
from app.models import Base, OutboxEvent, Notification
from app.workers import main as worker


@pytest_asyncio.fixture
async def client(tmp_path):
    db_path = tmp_path / "taskflow_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.state.test_sessionmaker = Session
    from app import main as main_module
    main_module._rate_fallback.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
    if hasattr(app.state, "test_sessionmaker"):
        del app.state.test_sessionmaker
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def register_and_login(client, email, name):
    registered = await client.post("/api/v1/auth/register", json={
        "name": name, "email": email, "password": "StrongPassword123!"
    })
    assert registered.status_code == 201, registered.text
    response = await client.post("/api/v1/auth/login", json={
        "email": email, "password": "StrongPassword123!"
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_workspace_isolation_invitation_rbac_and_task_workflow(client):
    owner_headers = await register_and_login(client, "owner@example.com", "Owner")
    member_headers = await register_and_login(client, "member@example.com", "Member")

    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Alpha"}, headers=owner_headers
    )).json()
    workspace_id = workspace["id"]

    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": "member@example.com", "role": "MEMBER"},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text
    accepted = await client.post(
        f"/api/v1/workspaces/invitations/{invite.json()['id']}/accept",
        headers=member_headers,
    )
    assert accepted.status_code == 200, accepted.text

    member_project_denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/projects",
        json={"name": "Forbidden"},
        headers=member_headers,
    )
    assert member_project_denied.status_code == 403

    promoted = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{accepted.json()['user_id']}",
        json={"role": "ADMIN"},
        headers=owner_headers,
    )
    assert promoted.status_code == 200
    admin_project = await client.post(
        f"/api/v1/workspaces/{workspace_id}/projects",
        json={"name": "Admin project"},
        headers=member_headers,
    )
    assert admin_project.status_code == 201, admin_project.text
    demoted = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{accepted.json()['user_id']}",
        json={"role": "MEMBER"},
        headers=owner_headers,
    )
    assert demoted.status_code == 200

    project_response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/projects",
        json={"name": "Core"},
        headers=owner_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]

    # Member can create a task assigned to themselves, but cannot assign others.
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks",
        json={"project_id": project_id, "title": "Member task", "assignee_id": accepted.json()["user_id"]},
        headers={**member_headers, "Idempotency-Key": "member-task-1"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    invalid_transition = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/transition",
        json={"expected_version": 1, "status": "IN_REVIEW"},
        headers=member_headers,
    )
    assert invalid_transition.status_code == 403

    denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/assign",
        json={"expected_version": 1, "assignee_id": "some-other-user"},
        headers=member_headers,
    )
    assert denied.status_code == 403

    # Newly assigned tasks start in TODO unless their due date is already past.
    assert created.json()["status"] == "TODO"

    # Members cannot edit title, description, priority, due date or labels.
    member_edit_denied = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}",
        json={"expected_version": 1, "title": "Member must not rename this"},
        headers=member_headers,
    )
    assert member_edit_denied.status_code == 403

    member_comment = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/comments",
        json={"body": "I have started working on this."},
        headers=member_headers,
    )
    assert member_comment.status_code == 201, member_comment.text

    priority_denied = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}",
        json={"expected_version": 1, "priority": "HIGH"},
        headers=member_headers,
    )
    assert priority_denied.status_code == 403

    # Assigned member can move exactly one step forward: TODO -> IN_PROGRESS.
    forward = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/transition",
        json={"expected_version": 1, "status": "IN_PROGRESS"},
        headers=member_headers,
    )
    assert forward.status_code == 200, forward.text
    backward = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/transition",
        json={"expected_version": 2, "status": "TODO"},
        headers=member_headers,
    )
    assert backward.status_code == 403

    # Cross-workspace IDs are concealed from non-members.
    second_workspace = await client.post(
        "/api/v1/workspaces", json={"name": "Private"}, headers=member_headers
    )
    assert second_workspace.status_code == 201
    isolated = await client.get(
        f"/api/v1/workspaces/{second_workspace.json()['id']}",
        headers=owner_headers,
    )
    assert isolated.status_code == 404


@pytest.mark.asyncio
async def test_idempotency_replay_and_payload_mismatch(client):
    headers = await register_and_login(client, "idem@example.com", "Idem")
    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Idem"}, headers=headers
    )).json()
    workspace_id = workspace["id"]
    project = (await client.post(
        f"/api/v1/workspaces/{workspace_id}/projects",
        json={"name": "Project"}, headers=headers
    )).json()
    payload = {
        "project_id": project["id"], "title": "One",
        "assignee_id": workspace["created_by"], "labels": ["backend", "urgent"],
    }
    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks", json=payload,
        headers={**headers, "Idempotency-Key": "stable-key"},
    )
    replay = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks", json=payload,
        headers={**headers, "Idempotency-Key": "stable-key"},
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    assert first.json()["labels"] == ["backend", "urgent"]

    changed = {**payload, "title": "Different"}
    mismatch = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tasks", json=changed,
        headers={**headers, "Idempotency-Key": "stable-key"},
    )
    assert mismatch.status_code == 409


@pytest.mark.asyncio
async def test_dependency_cycles_and_blocked_completion(client):
    headers = await register_and_login(client, "deps@example.com", "Deps")
    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Deps"}, headers=headers
    )).json()
    wid = workspace["id"]
    project = (await client.post(
        f"/api/v1/workspaces/{wid}/projects", json={"name": "P"}, headers=headers
    )).json()
    async def make_task(title, key):
        response = await client.post(
            f"/api/v1/workspaces/{wid}/tasks",
            json={"project_id": project["id"], "title": title, "assignee_id": workspace["created_by"]},
            headers={**headers, "Idempotency-Key": key},
        )
        assert response.status_code == 201, response.text
        return response.json()

    blocked = await make_task("Blocked", "blocked")
    blocker = await make_task("Blocker", "blocker")
    dep = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{blocked['id']}/dependencies",
        json={"depends_on_task_id": blocker["id"]}, headers=headers,
    )
    assert dep.status_code == 201, dep.text

    cycle = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{blocker['id']}/dependencies",
        json={"depends_on_task_id": blocked["id"]}, headers=headers,
    )
    assert cycle.status_code == 400

    # Dependencies block execution, review and completion for every role.
    blocked_advance = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{blocked['id']}/transition",
        json={"expected_version": 2, "status": "IN_PROGRESS"}, headers=headers,
    )
    assert blocked_advance.status_code == 409
    assert "prerequisite" in blocked_advance.json()["detail"].lower()

    # Completing the prerequisite unblocks the dependent task.
    blocker_done = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{blocker['id']}/transition",
        json={"expected_version": 1, "status": "DONE"}, headers=headers,
    )
    assert blocker_done.status_code == 200, blocker_done.text
    unblocked_advance = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{blocked['id']}/transition",
        json={"expected_version": 2, "status": "IN_PROGRESS"}, headers=headers,
    )
    assert unblocked_advance.status_code == 200, unblocked_advance.text


@pytest.mark.asyncio
async def test_task_creation_status_depends_on_due_date(client):
    from datetime import datetime, timedelta, timezone

    headers = await register_and_login(client, "taskdates@example.com", "Task Dates")
    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Task Dates"}, headers=headers
    )).json()
    wid = workspace["id"]
    project = (await client.post(
        f"/api/v1/workspaces/{wid}/projects", json={"name": "Dates"}, headers=headers
    )).json()

    base = {"project_id": project["id"], "assignee_id": workspace["created_by"]}
    future = await client.post(
        f"/api/v1/workspaces/{wid}/tasks",
        json={**base, "title": "Future due date", "due_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()},
        headers={**headers, "Idempotency-Key": "date-future"},
    )
    assert future.status_code == 201, future.text
    assert future.json()["status"] == "TODO"

    past = await client.post(
        f"/api/v1/workspaces/{wid}/tasks",
        json={**base, "title": "Past due date", "due_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()},
        headers={**headers, "Idempotency-Key": "date-past"},
    )
    assert past.status_code == 201, past.text
    assert past.json()["status"] == "BACKLOG"


@pytest.mark.asyncio
async def test_refresh_rotation_logout_and_invalid_login(client):
    await client.post("/api/v1/auth/register", json={
        "name": "Auth", "email": "auth@example.com", "password": "StrongPassword123!"
    })
    invalid = await client.post("/api/v1/auth/login", json={
        "email": "auth@example.com", "password": "wrong-password"
    })
    assert invalid.status_code == 401

    login = await client.post("/api/v1/auth/login", json={
        "email": "auth@example.com", "password": "StrongPassword123!"
    })
    assert login.status_code == 200
    first_refresh = client.cookies.get("taskflow_refresh")
    rotated = await client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200
    second_refresh = client.cookies.get("taskflow_refresh")
    assert first_refresh and second_refresh and first_refresh != second_refresh

    # A revoked refresh token cannot be replayed.
    client.cookies.set("taskflow_refresh", first_refresh)
    replay = await client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401

    client.cookies.set("taskflow_refresh", second_refresh)
    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    after_logout = await client.post("/api/v1/auth/refresh")
    assert after_logout.status_code == 401


@pytest.mark.asyncio
async def test_worker_notification_deduplication_and_retry_backoff(client, monkeypatch):
    headers = await register_and_login(client, "worker@example.com", "Worker")
    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Worker"}, headers=headers
    )).json()
    wid = workspace["id"]
    project = (await client.post(
        f"/api/v1/workspaces/{wid}/projects",
        json={"name": "Worker project"}, headers=headers
    )).json()
    task_response = await client.post(
        f"/api/v1/workspaces/{wid}/tasks",
        json={
            "project_id": project["id"],
            "title": "Worker task",
            "assignee_id": workspace["created_by"],
        },
        headers={**headers, "Idempotency-Key": "worker-task"},
    )
    assert task_response.status_code == 201, task_response.text
    task_id = task_response.json()["id"]

    async with app.state.test_sessionmaker() as db:
        event = OutboxEvent(
            event_type="task.created",
            aggregate_id=task_id,
            payload={
                "workspace_id": wid,
                "assignee_id": workspace["created_by"],
                "title": "Worker task",
                "version": 1,
            },
        )
        db.add(event)
        await db.flush()
        await worker.process_event(db, event)
        await db.commit()
        await worker.process_event(db, event)
        await db.commit()

        count = await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.recipient_id == workspace["created_by"],
                Notification.event_key == f"task-assigned:{task_id}",
            )
        )
        assert count == 1

    # Force one worker processing failure and verify durable retry metadata.
    async with app.state.test_sessionmaker() as db:
        retry_event = OutboxEvent(
            event_type="test.retry",
            aggregate_id="retry-aggregate",
            payload={},
        )
        db.add(retry_event)
        await db.commit()
        retry_id = retry_event.id

    monkeypatch.setattr(worker, "SessionLocal", app.state.test_sessionmaker)

    async def fail_once(db, event):
        raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(worker, "process_event", fail_once)
    await worker.process_batch()

    async with app.state.test_sessionmaker() as db:
        retried = await db.get(OutboxEvent, retry_id)
        assert retried.status == "RETRY"
        assert retried.attempts == 1
        assert retried.last_error == "simulated transient failure"
        assert retried.available_at is not None


@pytest.mark.asyncio
async def test_rate_limit_fallback_and_request_id(client, monkeypatch):
    from app.core.redis import redis_client

    async def redis_unavailable(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(redis_client, "incr", redis_unavailable)
    responses = []
    for _ in range(10):
        responses.append(await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "WrongPassword123!"},
        ))
    assert all(response.status_code == 401 for response in responses)

    limited = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "WrongPassword123!"},
    )
    assert limited.status_code == 429
    assert limited.headers.get("retry-after") == "60"

    health = await client.get("/health", headers={"X-Request-ID": "trace-test-123"})
    assert health.status_code == 200
    assert health.headers.get("x-request-id") == "trace-test-123"


@pytest.mark.asyncio
async def test_failed_task_validation_leaves_no_partial_business_records(client):
    from app.models import Task, AuditEvent

    headers = await register_and_login(client, "atomic@example.com", "Atomic")
    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Atomic"}, headers=headers
    )).json()
    wid = workspace["id"]
    project = (await client.post(
        f"/api/v1/workspaces/{wid}/projects",
        json={"name": "Atomic project"}, headers=headers
    )).json()

    rejected = await client.post(
        f"/api/v1/workspaces/{wid}/tasks",
        json={
            "project_id": project["id"],
            "title": "Should not exist",
            "assignee_id": "not-a-workspace-member",
        },
        headers={**headers, "Idempotency-Key": "invalid-assignee"},
    )
    assert rejected.status_code == 400

    async with app.state.test_sessionmaker() as db:
        task_count = await db.scalar(select(func.count()).select_from(Task))
        audit_count = await db.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "TASK_CREATED"
            )
        )
        assert task_count == 0
        assert audit_count == 0


@pytest.mark.asyncio
async def test_task_creation_dependencies_are_atomic_and_cycles_check_full_chain(client):
    from app.models import Task, TaskDependency

    headers = await register_and_login(client, "longchain@example.com", "Long chain")
    workspace = (await client.post(
        "/api/v1/workspaces", json={"name": "Long chain"}, headers=headers
    )).json()
    wid = workspace["id"]
    project = (await client.post(
        f"/api/v1/workspaces/{wid}/projects", json={"name": "Graph"}, headers=headers
    )).json()

    async def create_task(title, key, depends_on=None):
        payload = {
            "project_id": project["id"], "title": title,
            "assignee_id": workspace["created_by"],
            "depends_on_task_ids": depends_on or [],
        }
        response = await client.post(
            f"/api/v1/workspaces/{wid}/tasks", json=payload,
            headers={**headers, "Idempotency-Key": key},
        )
        return response

    a = await create_task("Task A", "graph-a")
    assert a.status_code == 201, a.text
    b = await create_task("Task B", "graph-b", [a.json()["id"]])
    assert b.status_code == 201, b.text
    c = await create_task("Task C", "graph-c", [b.json()["id"]])
    assert c.status_code == 201, c.text
    d = await create_task("Task D", "graph-d", [c.json()["id"]])
    assert d.status_code == 201, d.text

    # D -> C -> B -> A already exists; adding A -> D closes a four-node cycle.
    cycle = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{a.json()['id']}/dependencies",
        json={"depends_on_task_id": d.json()["id"]},
        headers=headers,
    )
    assert cycle.status_code == 400
    assert "circular" in cycle.json()["detail"].lower()

    # Invalid prerequisite IDs must not leave the just-created task behind.
    async with app.state.test_sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(Task))
    invalid = await create_task("Invalid task", "graph-invalid", ["missing-task-id"])
    assert invalid.status_code == 400
    async with app.state.test_sessionmaker() as db:
        after = await db.scalar(select(func.count()).select_from(Task))
        dep_count = await db.scalar(select(func.count()).select_from(TaskDependency))
    assert after == before
    assert dep_count == 3

    # Dependency gates work through a multi-task chain, not just a single edge.
    d_blocked = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{d.json()['id']}/transition",
        json={"expected_version": 1, "status": "IN_PROGRESS"}, headers=headers,
    )
    assert d_blocked.status_code == 409

    # Complete the chain in prerequisite order: A -> B -> C -> D.
    for prerequisite in (a, b, c):
        completed = await client.post(
            f"/api/v1/workspaces/{wid}/tasks/{prerequisite.json()['id']}/transition",
            json={"expected_version": 1, "status": "DONE"}, headers=headers,
        )
        assert completed.status_code == 200, completed.text

    d_unblocked = await client.post(
        f"/api/v1/workspaces/{wid}/tasks/{d.json()['id']}/transition",
        json={"expected_version": 1, "status": "IN_PROGRESS"}, headers=headers,
    )
    assert d_unblocked.status_code == 200, d_unblocked.text
