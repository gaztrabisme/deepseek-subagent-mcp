"""The escalation ladder, and where the supervisor gets its facts."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from mcp.types import ClientCapabilities

from deepseek_subagent_mcp.guard import ALLOW, DENY
from deepseek_subagent_mcp.supervisor import (
    TIER_DETERMINISTIC,
    TIER_ELICITATION,
    TIER_SAMPLING,
    Supervisor,
)

from .test_runs import make_settings

# anyio ships the async-test plugin; mcp already depends on it, so no extra dev
# dependency is needed just to await a coroutine in a test.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    """A client advertising whichever capabilities the test names."""

    def __init__(self, sampling=False, elicitation=False, model=None, human=None):
        self._sampling = sampling
        self._elicitation = elicitation
        self._model = model
        self._human = human
        self.asked: list[str] = []

    def check_client_capability(self, capability: ClientCapabilities) -> bool:
        if capability.sampling is not None:
            return self._sampling
        if capability.elicitation is not None:
            return self._elicitation
        return False

    async def create_message(self, **kwargs):
        self.asked.append("sampling")
        return await self._model(**kwargs)

    async def elicit_form(self, **kwargs):
        self.asked.append("elicitation")
        return await self._human(**kwargs)


class Reply:
    def __init__(self, text: str) -> None:
        self.content = type("C", (), {"text": text})()


class Elicited:
    def __init__(self, allow: bool) -> None:
        self.action = "accept"
        self.content = {"allow": allow}


async def allow_model(**kwargs):
    return Reply("ALLOW\nlooks fine")


async def deny_model(**kwargs):
    return Reply("DENY\nno")


async def broken_model(**kwargs):
    raise RuntimeError("the sampling capability was removed")


async def slow_model(**kwargs):
    await anyio.sleep(30)


async def allow_human(**kwargs):
    return Elicited(True)


# An unrecognised program: the policy has no rule that settles it either way,
# which is exactly what escalation is for. (A compound line of read-only
# commands is NOT this -- policy now settles those segment by segment.)
ESCALATES = ("bash", {"command": "some-unknown-binary --go"})


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(tmp_path, supervisor="auto", supervisor_timeout=0.5)


def bound(settings, session, registry=None) -> Supervisor:
    supervisor = Supervisor(settings, registry)
    supervisor.bind(session)
    return supervisor


# --- tier resolution -------------------------------------------------------


@pytest.mark.parametrize("sampling,elicitation,expected", [
    (True, True, TIER_SAMPLING),
    (False, True, TIER_ELICITATION),
    (True, False, TIER_SAMPLING),
    (False, False, TIER_DETERMINISTIC),
])
def test_the_tier_is_the_best_capability_the_client_offers(
    settings, sampling, elicitation, expected
):
    supervisor = bound(settings, FakeSession(sampling=sampling, elicitation=elicitation))
    assert supervisor.tier == expected


def test_supervisor_off_never_escalates(tmp_path):
    settings = make_settings(tmp_path, supervisor="off")
    supervisor = bound(settings, FakeSession(sampling=True, elicitation=True))
    assert supervisor.tier == TIER_DETERMINISTIC


# --- walking the ladder ----------------------------------------------------


async def test_a_failing_tier_falls_through_to_the_next(settings, tmp_path):
    """Sampling is deprecated; a client that drops it must not deny everything."""
    session = FakeSession(sampling=True, elicitation=True,
                          model=broken_model, human=allow_human)
    supervisor = bound(settings, session)
    verdict = await supervisor.decide(*ESCALATES, tmp_path)
    assert session.asked == ["sampling", "elicitation"]
    assert verdict.action == ALLOW
    assert supervisor.decisions[-1]["tier"] == TIER_ELICITATION


async def test_a_timeout_denies_without_asking_the_next_tier(settings, tmp_path):
    """An unanswered question is a no. Re-asking elsewhere only doubles the wait."""
    session = FakeSession(sampling=True, elicitation=True,
                          model=slow_model, human=allow_human)
    supervisor = bound(settings, session)
    verdict = await supervisor.decide(*ESCALATES, tmp_path)
    assert session.asked == ["sampling"]
    assert verdict.action == DENY
    assert "did not answer" in verdict.reason


async def test_every_tier_failing_denies(settings, tmp_path):
    session = FakeSession(sampling=True, model=broken_model)
    supervisor = bound(settings, session)
    verdict = await supervisor.decide(*ESCALATES, tmp_path)
    assert verdict.action == DENY
    assert "unavailable" in verdict.reason


async def test_no_capability_at_all_denies(settings, tmp_path):
    supervisor = bound(settings, FakeSession())
    verdict = await supervisor.decide(*ESCALATES, tmp_path)
    assert verdict.action == DENY
    assert "fails closed" in verdict.reason


async def test_the_supervisors_answer_decides(settings, tmp_path):
    session = FakeSession(sampling=True, model=deny_model)
    verdict = await bound(settings, session).decide(*ESCALATES, tmp_path)
    assert verdict.action == DENY
    assert "supervisor denied" in verdict.reason


async def test_policy_settles_the_clear_cases_without_asking_anyone(settings, tmp_path):
    session = FakeSession(sampling=True, model=allow_model)
    supervisor = bound(settings, session)
    verdict = await supervisor.decide("bash", {"command": "ls -la"}, tmp_path)
    assert verdict.action == ALLOW
    assert session.asked == [], "a read-only command must cost no model call"
    assert supervisor.decisions[-1]["tier"] == "policy"


async def test_allow_escalations_is_a_benchmarking_mode_only(tmp_path):
    settings = make_settings(tmp_path, supervisor="allow-escalations")
    session = FakeSession(sampling=True, model=deny_model)
    verdict = await bound(settings, session).decide(*ESCALATES, tmp_path)
    assert verdict.action == ALLOW
    assert session.asked == []


# --- where the facts come from ---------------------------------------------


class FakeAgent:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace


class FakeRegistry:
    def __init__(self, agents: dict) -> None:
        self._agents = agents

    def find_agent(self, agent_id: str):
        return self._agents.get(agent_id)


def test_the_workspace_comes_from_the_registry_not_the_request(settings, tmp_path):
    """The hook reports a workspace, but the hook is on the supervised side."""
    real = tmp_path / "real"
    real.mkdir()
    supervisor = bound(settings, FakeSession(), FakeRegistry({"a1": FakeAgent(real)}))
    assert supervisor.workspace_for("a1", "/somewhere/else") == real


def test_an_unknown_agent_falls_back_to_the_server_workspace(settings, tmp_path):
    supervisor = bound(settings, FakeSession(), FakeRegistry({}))
    assert supervisor.workspace_for("ghost", "/somewhere/else") == settings.workspace


async def test_decisions_are_attributed_to_their_agent(settings, tmp_path):
    supervisor = bound(settings, FakeSession())
    await supervisor.decide("bash", {"command": "ls"}, tmp_path, agent_id="a7")
    assert supervisor.decisions[-1]["agent_id"] == "a7"
    assert supervisor.decisions[-1]["tool"] == "bash"
