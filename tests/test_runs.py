"""Registry behaviour, with the harness subprocess replaced by a fake."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from deepseek_subagent_mcp import runs as runs_mod
from deepseek_subagent_mcp.config import Settings
from deepseek_subagent_mcp.runs import (
    CANCELLED,
    COMPLETED,
    COMPLETED_UNVERIFIED,
    FAILED,
    Registry,
    RegistryError,
    summarize_notification,
    tool_call_signature,
    turn_error_detail,
)


class FakeNotification:
    def __init__(self, method: str, payload: dict) -> None:
        self.method = method
        self.payload = payload


class FakeResult:
    def __init__(self, text: str, reason: str | None) -> None:
        self.final_response = text
        self.finish_reason = reason
        self.session_id = "s"


class FakeSession:
    def __init__(self, harness: FakeHarness) -> None:
        self.harness = harness

    def run(self, prompt: str, *, on_notification=None):
        self.harness.prompts.append(prompt)
        if on_notification is not None:
            on_notification(
                FakeNotification(
                    "session.event",
                    {"event": {"type": "tool/call", "data": {"name": "bash"}}},
                )
            )
        # Block until released so cancellation has something to interrupt.
        self.harness.gate.wait(timeout=10)
        if self.harness.raise_transport:
            from deepseek_harness.errors import TransportClosedError

            raise TransportClosedError("runtime exited")
        return FakeResult(f"answer to: {prompt}", self.harness.reason)


class FakeHarness:
    instances: list[FakeHarness] = []

    def __init__(self, config) -> None:
        self.config = config
        self.prompts: list[str] = []
        self.gate = threading.Event()
        self.gate.set()
        self.reason: str | None = "completed"
        self.raise_transport = False
        self.closed = False
        FakeHarness.instances.append(self)

    def start_session(self, session_id: str) -> FakeSession:
        self.session_id = session_id
        return FakeSession(self)

    def close(self) -> None:
        self.closed = True
        self.raise_transport = True
        self.gate.set()


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "provider": "deepseek-official",
        "model": "deepseek-v4-pro",
        "max_tokens": None,
        "workspace": tmp_path,
        "session_root": tmp_path / ".dsh-sessions",
        "cordis": None,
        "max_agents": 2,
        "request_timeout_seconds": None,
        "transcript_limit": 50,
        "api_key": "test-key",
        "base_url": None,
        "reasoning_effort": "low",
        "context_window": 200_000,
        "bash_timeout_ms": 60_000,
        "sandbox_mode": "workspace-write",
        "summary_tokens": 2000,
        "idle_timeout": 900.0,
        "run_timeout": 1800.0,
        "turn_token_budget": None,
        "loop_strikes": 3,
        "supervisor": "off",
        "supervisor_timeout": 120.0,
        "approval_socket": str(tmp_path / "approval.sock"),
        "log_level": "critical",
        "max_steps": 40,
        "verify_timeout": 30.0,
        "chars_per_token": 3.5,
        "run_archive": 200,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture(autouse=True)
def fake_harness(monkeypatch):
    FakeHarness.instances.clear()
    monkeypatch.setattr(runs_mod, "DeepSeekHarness", FakeHarness)
    monkeypatch.setattr(runs_mod, "DeepSeekHarnessConfig", lambda **kw: kw)
    yield


def wait_for(run, timeout=5.0):
    assert run.done.wait(timeout), f"run stuck in {run.state}"
    return run


def test_delegate_runs_and_returns_final_response(settings):
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent("worker", settings.workspace, settings.model)
    assert agent.wait_ready(5) is None
    run = wait_for(agent.submit("do the thing", verification="true"))
    assert run.state == COMPLETED
    assert run.final_response == "answer to: do the thing"
    assert run.finish_reason == "completed"
    assert "tool/call: bash" in list(run.transcript)
    agent.close()


def test_continue_reuses_one_session(settings):
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    wait_for(agent.submit("first"))
    wait_for(agent.submit("second"))
    harness = FakeHarness.instances[-1]
    assert harness.prompts == ["first", "second"]
    assert harness.session_id == agent.session_id
    assert len(FakeHarness.instances) == 1  # one process for both turns
    agent.close()


def test_unhappy_finish_reason_is_a_failure(settings):
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.submit("warmup")
    time.sleep(0.2)
    FakeHarness.instances[-1].reason = "max-tokens"
    run = wait_for(agent.submit("too long"))
    assert run.state == FAILED
    assert "max-tokens" in (run.error or "")
    agent.close()


def test_cancel_interrupts_the_in_flight_run(settings):
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.submit("warmup")
    time.sleep(0.2)
    harness = FakeHarness.instances[-1]
    harness.gate.clear()
    run = agent.submit("long job")
    time.sleep(0.2)
    agent.close()
    wait_for(run)
    assert run.state == CANCELLED
    assert harness.closed
    assert agent.closed


def test_closed_agent_refuses_new_work(settings):
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.close()
    with pytest.raises(RegistryError):
        agent.submit("nope")


def test_agent_limit_is_enforced(settings):
    registry = Registry(settings, start_reaper=False)
    registry.create_agent(None, settings.workspace, settings.model)
    registry.create_agent(None, settings.workspace, settings.model)
    with pytest.raises(RegistryError, match="agent limit"):
        registry.create_agent(None, settings.workspace, settings.model)
    registry.shutdown()


def test_find_run_and_unknown_ids(settings):
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("hello"))
    found_run = registry.find_run(run.run_id)
    assert found_run is run and found_run.agent_id == agent.agent_id
    with pytest.raises(RegistryError):
        registry.find_run("run-nope")
    with pytest.raises(RegistryError):
        registry.agent("a999")
    registry.shutdown()


def test_startup_failure_lands_on_the_run(settings, monkeypatch):
    def boom(_config):
        raise RuntimeError("no runtime binary")

    monkeypatch.setattr(runs_mod, "DeepSeekHarness", boom)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    assert "no runtime binary" in (agent.wait_ready(5) or "")
    with pytest.raises(RegistryError, match="failed to start"):
        agent.submit("anything")


@pytest.mark.parametrize(
    ("method", "payload", "expected"),
    [
        ("session.status", {"status": "idle"}, "status: idle"),
        (
            "session.event",
            {"event": {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}},
            "turn/end: completed",
        ),
        (
            "session.event",
            {
                "event": {
                    "type": "assistant/message",
                    "data": {"message": {"content": [{"type": "text", "text": " hi  there "}]}},
                }
            },
            "assistant: hi there",
        ),
        ("session.event", {"event": {"type": "agent/inbox/spliced"}}, "agent/inbox/spliced"),
        ("session.event", {"event": {"type": "assistant/chunk"}}, None),
        ("session.event", {"event": {"type": "request/context"}}, None),
        (
            "session.event",
            {"event": {"type": "assistant/message", "data": {"content": []}}},
            None,
        ),
        ("something/else", {}, None),
    ],
)
def test_summarize_notification(method, payload, expected):
    assert summarize_notification(FakeNotification(method, payload)) == expected


TURN_END_ERROR = FakeNotification(
    "session.event",
    {
        "event": {
            "type": "turn/end",
            "data": {
                "reason": {
                    "kind": "error",
                    "error": {"code": "MISSING_CREDENTIAL", "message": "no API key"},
                }
            },
        }
    },
)


def test_turn_error_detail_extracts_code_and_message():
    assert turn_error_detail(TURN_END_ERROR) == "MISSING_CREDENTIAL: no API key"
    assert summarize_notification(TURN_END_ERROR) == "turn/end: error (MISSING_CREDENTIAL)"


def test_turn_error_detail_ignores_unrelated_notifications():
    assert turn_error_detail(FakeNotification("session.status", {"status": "idle"})) is None
    assert (
        turn_error_detail(
            FakeNotification("session.event", {"event": {"type": "assistant/message"}})
        )
        is None
    )


def test_runtime_error_message_reaches_the_run(settings, monkeypatch):
    """A failing turn reports the runtime's own cause, not just the finish_reason."""

    original = FakeSession.run

    def run_with_error(self, prompt, *, on_notification=None):
        if on_notification is not None:
            on_notification(TURN_END_ERROR)
        self.harness.reason = "error"
        return original(self, prompt, on_notification=None)

    monkeypatch.setattr(FakeSession, "run", run_with_error)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("go"))
    assert run.state == FAILED
    assert run.error == "MISSING_CREDENTIAL: no API key"
    agent.close()


# --- reaping, deadlines, and loop detection --------------------------------


def tool_call(name="bash", args=None):
    data = {"name": name, "arguments": args or {"cmd": "ls"}}
    return FakeNotification("session.event", {"event": {"type": "tool/call", "data": data}})


def test_tool_call_signature_is_stable_and_argument_sensitive():
    a = tool_call_signature(tool_call("bash", {"cmd": "ls"}))
    b = tool_call_signature(tool_call("bash", {"cmd": "ls"}))
    c = tool_call_signature(tool_call("bash", {"cmd": "pwd"}))
    d = tool_call_signature(tool_call("write", {"cmd": "ls"}))
    assert a == b and a != c and a != d
    assert a.startswith("bash:")
    assert tool_call_signature(FakeNotification("session.status", {"status": "idle"})) is None


def test_idle_agent_is_reaped_and_evicted(tmp_path):
    settings = make_settings(tmp_path, idle_timeout=0.01)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    wait_for(agent.submit("work"))
    time.sleep(0.05)
    acted = registry.reap_once()
    assert (agent.agent_id, "idle") in acted
    assert agent.closed
    assert registry.agents() == []  # evicted, so dsh_list stays bounded


def test_capacity_recovers_after_reaping(tmp_path):
    """Today's deadlock: without eviction the second delegate fails forever."""
    settings = make_settings(tmp_path, max_agents=1, idle_timeout=0.01)
    registry = Registry(settings, start_reaper=False)
    first = registry.create_agent(None, settings.workspace, settings.model)
    wait_for(first.submit("one"))
    with pytest.raises(RegistryError, match="agent limit"):
        registry.create_agent(None, settings.workspace, settings.model)
    time.sleep(0.05)
    registry.reap_once()
    second = registry.create_agent(None, settings.workspace, settings.model)
    assert wait_for(second.submit("two", verification="true")).state == COMPLETED
    second.close()


def test_run_deadline_kills_the_agent(tmp_path):
    settings = make_settings(tmp_path, run_timeout=0.05)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.submit("warmup")
    time.sleep(0.2)
    FakeHarness.instances[-1].gate.clear()
    run = agent.submit("hangs forever")
    time.sleep(0.3)
    acted = registry.reap_once()
    assert (agent.agent_id, "timeout") in acted
    wait_for(run)
    assert run.state == FAILED
    assert "DSA_RUN_TIMEOUT" in (run.error or "")


def test_loop_detection_trips_and_kills(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, loop_strikes=3)
    original = FakeSession.run

    def looping(self, prompt, *, on_notification=None):
        if on_notification is not None:
            for _ in range(4):
                on_notification(tool_call("bash", {"cmd": "ls"}))
        return original(self, prompt, on_notification=None)

    monkeypatch.setattr(FakeSession, "run", looping)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.submit("warmup")
    time.sleep(0.2)
    FakeHarness.instances[-1].gate.clear()
    run = agent.submit("loops")
    time.sleep(0.2)
    assert run.loop_tripped is not None
    acted = registry.reap_once()
    assert (agent.agent_id, "loop") in acted
    wait_for(run)
    assert run.state == FAILED
    assert "runaway loop" in (run.error or "")


def test_a_kill_outranks_a_successful_result(tmp_path):
    """A killed process's output is never read as success."""
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("fine", verification="true"))
    assert run.state == COMPLETED
    agent._kill_kind = "timeout"
    agent.close("late timeout", kind="timeout")
    # A run that already settled keeps its verdict; the guard applies to runs
    # settling after the kill, which the deadline test covers.
    assert run.state == COMPLETED


def test_child_env_reaches_the_harness_config(tmp_path):
    settings = make_settings(tmp_path, reasoning_effort="max", sandbox_mode="read-only")
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    assert agent.wait_ready(5) is None
    env = FakeHarness.instances[-1].config["env"]
    assert env["DSA_REASONING_EFFORT"] == "max"
    assert env["DSA_SANDBOX_MODE"] == "read-only"
    assert env["DSA_CONTEXT_WINDOW"] == "200000"
    agent.close()


# --- usage accounting, ceilings, archive, distillation ---------------------


def usage_event(input_tokens=100, output_tokens=10, cache_read=0, reasoning=0):
    return FakeNotification(
        "session.event",
        {"event": {"type": "assistant/message", "data": {
            "turn": 1, "step": 1,
            "message": {"content": [{"type": "text", "text": "hi"}]},
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadTokens": cache_read,
                "reasoningTokens": reasoning,
            },
        }}},
    )


def step_event(step=1):
    return FakeNotification(
        "session.event", {"event": {"type": "step/start", "data": {"turn": 1, "step": step}}}
    )


def emitting(*notifications):
    """A FakeSession.run that emits the given notifications before answering."""
    original = FakeSession.run

    def run(self, prompt, *, on_notification=None):
        if on_notification is not None:
            for notification in notifications:
                on_notification(notification)
        return original(self, prompt, on_notification=None)

    return run


def test_usage_is_accumulated_from_assistant_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FakeSession, "run",
        emitting(step_event(1), usage_event(100, 10, cache_read=5, reasoning=7),
                 step_event(2), usage_event(200, 20)),
    )
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("work"))
    assert run.usage.input == 300
    assert run.usage.output == 30
    assert run.usage.cache_read == 5
    assert run.usage.steps == 2
    # Reasoning tokens are inside output and are never added to the total again.
    assert run.usage.reasoning == 7
    assert run.usage.total == 300 + 5 + 30
    assert agent.usage().total == run.usage.total
    agent.close()


def test_token_budget_trips_and_kills(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeSession, "run", emitting(usage_event(5000, 100)))
    settings = make_settings(tmp_path, turn_token_budget=1000)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.submit("warmup")
    time.sleep(0.2)
    FakeHarness.instances[-1].gate.clear()
    run = agent.submit("expensive")
    time.sleep(0.2)
    assert run.trip is not None and run.trip[0] == "budget"
    assert (agent.agent_id, "budget") in registry.reap_once()
    wait_for(run)
    assert run.state == FAILED
    assert "DSA_TURN_TOKEN_BUDGET" in (run.error or "")


def test_step_ceiling_trips_and_kills(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeSession, "run", emitting(*[step_event(i) for i in range(1, 6)]))
    settings = make_settings(tmp_path, max_steps=3)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    agent.submit("warmup")
    time.sleep(0.2)
    FakeHarness.instances[-1].gate.clear()
    run = agent.submit("many steps")
    time.sleep(0.2)
    assert run.trip is not None and run.trip[0] == "steps"
    assert (agent.agent_id, "steps") in registry.reap_once()
    wait_for(run)
    assert run.state == FAILED
    assert "DSA_MAX_STEPS" in (run.error or "")


def test_a_finished_run_survives_its_agent_being_reaped(tmp_path):
    """Delegate, go away, come back: the result must still be readable."""
    settings = make_settings(tmp_path, idle_timeout=0.01)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("do the thing", verification="true"))
    time.sleep(0.05)
    assert (agent.agent_id, "idle") in registry.reap_once()
    assert registry.agents() == []
    found = registry.find_run(run.run_id)
    assert found is run
    assert found.state == COMPLETED
    assert found.result_text == "answer to: do the thing"
    with pytest.raises(RegistryError, match="unknown agent_id"):
        registry.agent(agent.agent_id)


def test_the_archive_is_bounded(tmp_path):
    settings = make_settings(tmp_path, idle_timeout=0.01, run_archive=2, max_agents=5)
    registry = Registry(settings, start_reaper=False)
    ids = []
    for _ in range(3):
        agent = registry.create_agent(None, settings.workspace, settings.model)
        ids.append(wait_for(agent.submit("x", verification="true")).run_id)
        time.sleep(0.05)
        registry.reap_once()
    assert len(registry.archived_runs()) == 2
    with pytest.raises(RegistryError):
        registry.find_run(ids[0])  # the oldest was dropped
    assert registry.find_run(ids[2]).state == COMPLETED


def test_a_short_answer_is_returned_verbatim(tmp_path):
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("brief"))
    assert run.result_text == run.final_response
    assert run.distilled is False and run.truncated is False
    # One turn only: no distillation turn was spent.
    assert FakeHarness.instances[-1].prompts == ["brief"]
    agent.close()


def test_an_oversized_answer_is_distilled_in_the_same_session(tmp_path, monkeypatch):
    original = FakeSession.run

    def verbose(self, prompt, *, on_notification=None):
        result = original(self, prompt, on_notification=on_notification)
        if prompt.startswith("Stop working"):
            result.final_response = "## Goal\nshort handoff"
        else:
            result.final_response = "x" * 5000
        return result

    monkeypatch.setattr(FakeSession, "run", verbose)
    settings = make_settings(tmp_path, summary_tokens=100, chars_per_token=4.0)  # cap 400
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("verbose task"))
    assert run.distilled is True
    assert run.result_text == "## Goal\nshort handoff"
    assert run.final_response == "x" * 5000  # the raw answer is kept
    prompts = FakeHarness.instances[-1].prompts
    assert len(prompts) == 2 and prompts[1].startswith("Stop working")
    agent.close()


def test_an_oversized_summary_is_truncated_with_a_pointer(tmp_path, monkeypatch):
    original = FakeSession.run

    def verbose(self, prompt, *, on_notification=None):
        result = original(self, prompt, on_notification=on_notification)
        result.final_response = "y" * 5000
        return result

    monkeypatch.setattr(FakeSession, "run", verbose)
    settings = make_settings(tmp_path, summary_tokens=100, chars_per_token=4.0)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("verbose"))
    assert run.truncated is True
    assert len(run.result_text) <= settings.result_cap_chars
    assert "dsh_transcript" in run.result_text
    agent.close()


def test_verification_decides_the_terminal_state(tmp_path):
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)

    passing = wait_for(agent.submit("task", verification="true"))
    assert passing.state == COMPLETED
    assert passing.verification_result is not None
    assert passing.verification_result.passed is True

    failing = wait_for(agent.submit("task", verification="ls /definitely-not-here"))
    assert failing.state == COMPLETED_UNVERIFIED
    assert "verification failed" in (failing.error or "")
    agent.close()


def test_an_unverified_run_is_not_reported_completed(tmp_path):
    """No verification command means no evidence, so no clean completion."""
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = wait_for(agent.submit("task"))
    assert run.state == COMPLETED_UNVERIFIED
    assert run.verification_result is not None
    assert run.verification_result.passed is False
    # Not an error, though: the work may well be fine, it just was not checked.
    assert run.error is None


def test_phases_are_reported(tmp_path):
    settings = make_settings(tmp_path)
    registry = Registry(settings, start_reaper=False)
    agent = registry.create_agent(None, settings.workspace, settings.model)
    run = agent.submit("task", verification="true")
    wait_for(run)
    assert run.phase == "done"
    assert run.summary()["state"] == COMPLETED
    agent.close()
