"""Pure workflow rules shared by API code and unit tests."""
from app.models import TaskStatus

ALLOWED_TRANSITIONS = {
    TaskStatus.BACKLOG: {TaskStatus.TODO},
    TaskStatus.TODO: {TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS},
    TaskStatus.IN_PROGRESS: {TaskStatus.TODO, TaskStatus.IN_REVIEW},
    TaskStatus.IN_REVIEW: {TaskStatus.IN_PROGRESS, TaskStatus.DONE},
    TaskStatus.DONE: set(),
}


def transition_allowed(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def would_create_cycle(graph: dict[str, set[str]], task_id: str, blocker_id: str) -> bool:
    """Edges point from a task to the tasks it depends on."""
    pending = [blocker_id]
    visited = set()
    while pending:
        node = pending.pop()
        if node == task_id:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(graph.get(node, ()))
    return False


DEPENDENCY_GATED_STATUSES = {
    TaskStatus.IN_PROGRESS,
    TaskStatus.IN_REVIEW,
    TaskStatus.DONE,
}

def dependencies_must_be_complete(target: TaskStatus) -> bool:
    """Whether entering target requires every prerequisite task to be DONE."""
    return target in DEPENDENCY_GATED_STATUSES
