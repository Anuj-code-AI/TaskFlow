import pytest
from app.models import TaskStatus
from app.services.workflow import transition_allowed, would_create_cycle, dependencies_must_be_complete


@pytest.mark.parametrize(
    "current,target,expected",
    [
        (TaskStatus.BACKLOG, TaskStatus.TODO, True),
        (TaskStatus.TODO, TaskStatus.BACKLOG, True),
        (TaskStatus.TODO, TaskStatus.IN_PROGRESS, True),
        (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW, True),
        (TaskStatus.IN_REVIEW, TaskStatus.DONE, True),
        (TaskStatus.BACKLOG, TaskStatus.DONE, False),
        (TaskStatus.DONE, TaskStatus.TODO, False),
        (TaskStatus.IN_REVIEW, TaskStatus.TODO, False),
    ],
)
def test_workflow_transitions(current, target, expected):
    assert transition_allowed(current, target) is expected


def test_dependency_cycle_detection():
    graph = {"A": {"B"}, "B": {"C"}, "C": set()}
    assert would_create_cycle(graph, "C", "A")
    assert not would_create_cycle(graph, "A", "D")


def test_direct_self_dependency_is_cycle():
    assert would_create_cycle({}, "A", "A")


def test_dependency_cycle_detection_across_long_chain():
    # A -> B -> C -> D; attempting D -> A must be rejected.
    graph = {
        "A": {"B"},
        "B": {"C"},
        "C": {"D"},
        "D": set(),
    }
    assert would_create_cycle(graph, "D", "A")
    assert not would_create_cycle(graph, "A", "E")


def test_dependency_cycle_detection_with_branching_graph():
    # A depends on B and C; both ultimately depend on D.
    graph = {
        "A": {"B", "C"},
        "B": {"D"},
        "C": {"E"},
        "E": {"D"},
        "D": set(),
    }
    assert would_create_cycle(graph, "D", "A")
    assert would_create_cycle(graph, "D", "E")
    assert not would_create_cycle(graph, "A", "F")


@pytest.mark.parametrize(
    "target,requires_complete_dependencies",
    [
        (TaskStatus.BACKLOG, False),
        (TaskStatus.TODO, False),
        (TaskStatus.IN_PROGRESS, True),
        (TaskStatus.IN_REVIEW, True),
        (TaskStatus.DONE, True),
    ],
)
def test_dependency_gate_applies_before_execution_review_and_completion(target, requires_complete_dependencies):
    assert dependencies_must_be_complete(target) is requires_complete_dependencies
