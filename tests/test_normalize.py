"""Unit tests for the AIOpsLab trace adapter (praxis/adapters/aiopslab/normalize.py)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from conftest import load_fixture
from praxis.adapters.aiopslab.normalize import (
    READ_ACTIONS,
    SUBMIT_ACTIONS,
    TraceFormatError,
    to_events,
)
from praxis.core.model import AccessType, TraceEvent, classify_shell_command


class FakeSessionItem:
    """Attribute-form stand-in for AIOpsLab's Pydantic ``SessionItem``."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def turn(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def action_turn(code: str, text: str = "Doing it.") -> dict[str, str]:
    """An assistant turn shaped like real agent output: prose + one block."""
    return turn("assistant", f"{text}\n```\n{code}\n```")


def single_event(code: str) -> TraceEvent:
    """Normalize a one-turn trace holding one assistant action."""
    events = to_events([action_turn(code)])
    assert len(events) == 1
    return events[0]


FIXTURE_NAMES = [
    "safe_localization.json",
    "safe_mitigation.json",
    "ordering_violation.json",
    "readonly_task_mutation.json",
    "repeated_failure_loop.json",
    "actions_after_submit.json",
    "no_submit.json",
    "unparseable_turns.json",
]


# ---------------------------------------------------------------------------
# Input forms and structural guarantees
# ---------------------------------------------------------------------------


def test_dict_and_attribute_forms_produce_identical_events() -> None:
    raw_turns = [
        ("system", "You are an SRE agent."),
        ("user", "Begin the task."),
        (
            "assistant",
            'Reading logs.\n```\nget_logs(namespace="ns", service="geo")\n```',
        ),
        ("env", "log line one"),
        ("assistant", 'Done.\n```\nsubmit(["geo"])\n```'),
    ]
    dict_trace = [turn(role, content) for role, content in raw_turns]
    attr_trace = [FakeSessionItem(role, content) for role, content in raw_turns]

    dict_events = to_events(dict_trace)
    attr_events = to_events(attr_trace)

    assert [dataclasses.asdict(e) for e in dict_events] == [
        dataclasses.asdict(e) for e in attr_events
    ]


def test_mixed_item_forms_in_one_trace() -> None:
    trace = [
        FakeSessionItem("system", "prompt"),
        turn("assistant", 'Go.\n```\nget_logs(namespace="ns", service="geo")\n```'),
    ]
    events = to_events(trace)
    assert events[0].role == "system"
    assert events[1].api_name == "get_logs"


def test_one_event_per_turn_with_positional_indices() -> None:
    trace = [
        turn("system", "prompt"),
        turn("user", "go"),
        action_turn('get_logs(namespace="ns", service="geo")'),
        turn("env", "result"),
        action_turn('submit(["geo"])'),
    ]
    events = to_events(trace)
    assert len(events) == len(trace)
    assert [e.index for e in events] == [0, 1, 2, 3, 4]
    assert [e.role for e in events] == [
        "system",
        "user",
        "assistant",
        "env",
        "assistant",
    ]


def test_empty_trace_yields_no_events() -> None:
    assert to_events([]) == []


def test_missing_role_or_content_raises_trace_format_error() -> None:
    with pytest.raises(TraceFormatError, match="1"):
        to_events([turn("system", "ok"), {"role": "assistant"}])
    with pytest.raises(TraceFormatError, match="0"):
        to_events([{"content": "orphan"}])

    class RoleOnly:
        role = "assistant"

    with pytest.raises(TraceFormatError, match="2"):
        to_events([turn("system", "a"), turn("user", "b"), RoleOnly()])
    with pytest.raises(TraceFormatError, match="0"):
        to_events([42])


def test_non_str_content_is_coerced_via_str() -> None:
    events = to_events([{"role": "env", "content": 42}])
    assert events[0].raw == "42"
    events = to_events([FakeSessionItem("env", None)])  # type: ignore[arg-type]
    assert events[0].raw == "None"


# ---------------------------------------------------------------------------
# Access classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api", sorted(READ_ACTIONS))
def test_read_actions_classified_read(api: str) -> None:
    event = single_event(f'{api}(namespace="ns", service="geo")')
    assert event.api_name == api
    assert event.access is AccessType.READ


@pytest.mark.parametrize("api", sorted(SUBMIT_ACTIONS))
def test_submit_actions_classified_submit(api: str) -> None:
    event = single_event(f'{api}(["geo"])')
    assert event.api_name == api
    assert event.access is AccessType.SUBMIT


def test_unknown_api_classified_unknown() -> None:
    event = single_event('frobnicate("geo")')
    assert event.api_name == "frobnicate"
    assert event.access is AccessType.UNKNOWN


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("kubectl scale deployment geo --replicas=3 -n ns", AccessType.WRITE),
        ("kubectl get pods -n ns", AccessType.READ),
        ("echo hello", AccessType.UNKNOWN),
        # Flag tokens must not be verb-matched as substrings.
        ("kubectl get pods --no-delete", AccessType.READ),
    ],
)
def test_exec_shell_access_matches_shared_classifier(
    command: str, expected: AccessType
) -> None:
    event = single_event(f'exec_shell("{command}")')
    assert event.api_name == "exec_shell"
    assert event.args == [command]
    assert event.kwargs == {}
    assert event.access is expected
    # The adapter must delegate to the shared constant-driven classifier.
    assert event.access is classify_shell_command(command)


def test_exec_shell_empty_command_is_unknown() -> None:
    event = single_event('exec_shell("")')
    assert event.api_name == "exec_shell"
    assert event.access is AccessType.UNKNOWN
    assert event.resource is None


# ---------------------------------------------------------------------------
# exec_shell command extraction (ResponseParser clone behavior)
# ---------------------------------------------------------------------------


def test_exec_shell_command_kwarg_prefix_is_stripped() -> None:
    event = single_event('exec_shell(command="kubectl get pods -n ns")')
    assert event.api_name == "exec_shell"
    assert event.args == ["kubectl get pods -n ns"]


def test_exec_shell_unescapes_escaped_quotes_and_backslashes() -> None:
    # Block text: exec_shell(command="echo \"hello world\"")
    event = single_event('exec_shell(command="echo \\"hello world\\"")')
    assert event.args == ['echo "hello world"']
    # Block text: exec_shell("echo a\\b") -> unescaped to a single backslash.
    event = single_event('exec_shell("echo a\\\\b")')
    assert event.args == ["echo a\\b"]


def test_exec_shell_single_quoted_command() -> None:
    event = single_event("exec_shell('kubectl describe deployment geo')")
    assert event.args == ["kubectl describe deployment geo"]
    assert event.access is AccessType.READ


def test_exec_shell_unquoted_command_is_non_action() -> None:
    event = single_event("exec_shell(some_variable)")
    assert event.api_name is None
    assert event.access is AccessType.UNKNOWN


# ---------------------------------------------------------------------------
# Non-action turns (never raise, always one event)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Pure reasoning, no code block at all.",
        'Two blocks.\n```\nget_logs(namespace="ns", service="geo")\n```\nand\n```\nget_metrics(namespace="ns", service="geo")\n```',
        "Truncated call.\n```\nget_logs(namespace=\n```",
        "Not a call.\n```\nx = 1\n```",
        "Two statements.\n```\nget_logs()\nget_metrics()\n```",
        "Attribute call.\n```\nclient.get_logs()\n```",
    ],
)
def test_unparseable_assistant_turns_become_non_action_events(content: str) -> None:
    events = to_events([turn("assistant", content)])
    assert len(events) == 1
    event = events[0]
    assert event.api_name is None
    assert event.access is AccessType.UNKNOWN
    assert event.args == [] and event.kwargs == {}
    assert event.resource is None
    assert event.raw == content


@pytest.mark.parametrize("role", ["system", "user", "env", "tool"])
def test_non_assistant_turns_are_never_parsed(role: str) -> None:
    content = 'Looks like code:\n```\nget_logs(namespace="ns", service="geo")\n```'
    events = to_events([turn(role, content)])
    event = events[0]
    assert event.role == role
    assert event.api_name is None
    assert event.access is AccessType.UNKNOWN
    assert event.raw == content


def test_language_tagged_fence_is_accepted() -> None:
    content = 'Run:\n```python\nget_logs(namespace="ns", service="geo")\n```'
    events = to_events([turn("assistant", content)])
    assert events[0].api_name == "get_logs"
    assert events[0].access is AccessType.READ


# ---------------------------------------------------------------------------
# args / kwargs extraction
# ---------------------------------------------------------------------------


def test_positional_and_keyword_args_extracted() -> None:
    event = single_event('get_metrics("ns", service="geo", duration=5)')
    assert event.args == ["ns"]
    assert event.kwargs == {"service": "geo", "duration": 5}


def test_list_and_dict_literal_args() -> None:
    event = single_event('submit(["geo", "rate"])')
    assert event.args == [["geo", "rate"]]
    event = single_event('submit({"system_level": "service", "fault_type": "config"})')
    assert event.args == [{"system_level": "service", "fault_type": "config"}]


def test_non_literal_args_fall_back_to_source_text() -> None:
    event = single_event("get_logs(namespace=ns_var)")
    assert event.api_name == "get_logs"
    assert event.kwargs == {"namespace": "ns_var"}


# ---------------------------------------------------------------------------
# Resource extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("kubectl scale deployment geo --replicas=3 -n ns", "geo"),
        ("kubectl delete pod geo-abc123 -n ns", "geo-abc123"),
        ("kubectl rollout restart deployment/geo", "geo"),
        ("kubectl rollout restart deployment.apps/geo", "geo"),
        ("kubectl describe deployment geo", "geo"),
        # Documented decision: no named resource -> namespace fallback.
        ("kubectl get pods -n test", "test"),
        ("echo hello", None),
    ],
)
def test_shell_resource_extraction(command: str, expected: str | None) -> None:
    event = single_event(f'exec_shell("{command}")')
    assert event.resource == expected


def test_api_resource_from_kwargs_priority() -> None:
    event = single_event('get_logs(namespace="ns", service="geo")')
    assert event.resource == "geo"
    # 'service' outranks 'pod' in the kwarg priority order.
    event = single_event('get_logs(pod="geo-abc", service="geo")')
    assert event.resource == "geo"
    event = single_event('get_logs(namespace="ns", pod_name="geo-abc")')
    assert event.resource == "geo-abc"


def test_api_resource_from_positional_args() -> None:
    # Two positional strings: AIOpsLab telemetry convention (namespace, service).
    event = single_event('get_logs("ns", "geo")')
    assert event.resource == "geo"
    # Single positional string on a READ action.
    event = single_event('get_logs("geo")')
    assert event.resource == "geo"
    # Single positional string on an unknown action: not derivable.
    event = single_event('frobnicate("geo")')
    assert event.resource is None
    # Non-string arg (submit list): not derivable.
    event = single_event('submit(["geo"])')
    assert event.resource is None


# ---------------------------------------------------------------------------
# Fixtures end-to-end through to_events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_normalizes_one_event_per_turn(name: str) -> None:
    fixture = load_fixture(name)
    assert fixture["task_type"] in {
        "detection",
        "localization",
        "analysis",
        "mitigation",
    }
    trace = fixture["trace"]
    events = to_events(trace)
    assert len(events) == len(trace)
    assert [e.index for e in events] == list(range(len(trace)))
    assert [e.role for e in events] == [t["role"] for t in trace]
    assert trace[0]["role"] == "system" and trace[1]["role"] == "user"


def test_safe_localization_fixture_shape() -> None:
    events = to_events(load_fixture("safe_localization.json")["trace"])
    assert all(e.access is not AccessType.WRITE for e in events)
    assert events[2].api_name == "get_logs" and events[2].access is AccessType.READ
    assert events[2].resource == "geo"
    assert events[6].api_name == "exec_shell" and events[6].access is AccessType.READ
    assert events[6].resource == "test-social-network"  # namespace fallback
    assert events[8].access is AccessType.SUBMIT
    assert events[8].args == [["geo"]]


def test_safe_mitigation_fixture_shape() -> None:
    events = to_events(load_fixture("safe_mitigation.json")["trace"])
    assert events[4].api_name == "exec_shell" and events[4].access is AccessType.READ
    assert events[4].resource == "geo"
    write = events[6]
    assert write.access is AccessType.WRITE and write.resource == "geo"
    # Reads on the same resource strictly precede the write.
    assert any(
        e.access is AccessType.READ and e.resource == "geo"
        for e in events[: write.index]
    )
    assert events[8].access is AccessType.SUBMIT and events[8].args == []


def test_ordering_violation_fixture_shape() -> None:
    events = to_events(load_fixture("ordering_violation.json")["trace"])
    write = events[2]
    assert write.api_name == "exec_shell"
    assert write.access is AccessType.WRITE
    assert write.resource == "geo"
    # The write is the first action event: nothing before it has an api_name.
    assert all(e.api_name is None for e in events[:2])
    assert all(e.access is not AccessType.READ for e in events[:2])


def test_readonly_task_mutation_fixture_shape() -> None:
    fixture = load_fixture("readonly_task_mutation.json")
    assert fixture["task_type"] == "localization"
    events = to_events(fixture["trace"])
    read, write = events[2], events[4]
    assert read.access is AccessType.READ
    assert write.access is AccessType.WRITE
    # Same-resource read precedes the delete (keeps ReadBeforeWrite silent).
    assert read.resource == write.resource == "geo-6d5f9b7c8-x2v4q"


def test_repeated_failure_loop_fixture_shape() -> None:
    events = to_events(load_fixture("repeated_failure_loop.json")["trace"])
    for idx in (2, 4, 6):
        assert events[idx].api_name == "get_logs"
        assert events[idx].kwargs["namespace"] == "test-hotel-res"
    for idx in (3, 5, 7):
        assert events[idx].role == "env"
        assert "does not exist" in events[idx].raw
    assert events[8].kwargs["namespace"] == "test-hotel-reservation"
    assert "Error" not in events[9].raw
    assert events[10].access is AccessType.SUBMIT


def test_actions_after_submit_fixture_shape() -> None:
    events = to_events(load_fixture("actions_after_submit.json")["trace"])
    submits = [e.index for e in events if e.access is AccessType.SUBMIT]
    assert submits == [4, 8]
    late_action = events[6]
    assert late_action.api_name == "get_logs"
    assert late_action.access is AccessType.READ


def test_no_submit_fixture_shape() -> None:
    events = to_events(load_fixture("no_submit.json")["trace"])
    assert not any(e.access is AccessType.SUBMIT for e in events)
    reads = [e for e in events if e.access is AccessType.READ]
    assert [e.api_name for e in reads] == ["get_logs", "get_metrics"]


def test_unparseable_turns_fixture_shape() -> None:
    events = to_events(load_fixture("unparseable_turns.json")["trace"])
    # No fence / two fences / truncated call: all non-action, none raised.
    for idx in (2, 3, 4):
        assert events[idx].role == "assistant"
        assert events[idx].api_name is None
        assert events[idx].access is AccessType.UNKNOWN
    assert "No API call found" in events[5].raw
    assert events[6].api_name == "get_traces" and events[6].access is AccessType.READ
    assert events[8].access is AccessType.SUBMIT
    assert events[8].args == [{"system_level": "service", "fault_type": "config"}]
