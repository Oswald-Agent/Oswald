"""CLI argument handling, production composition, and machine-readable outcome."""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from oswald.cli.__main__ import SavedPlanPlanner, main, run
from oswald.core.agents.coder import CoderAgent
from oswald.core.agents.reviewer import ReviewerAgent
from oswald.core.agents.tester import TesterAgent
from oswald.core.orchestrator import PlannerAdapter, RunResult
from oswald.core.sandbox.fake import FakeSandboxRuntime
from oswald.core.sandbox.models import SandboxConfig
from oswald.core.vcs.workspace import FakeWorkspaceDiff


def test_cli_defaults_to_real_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["oswald", "Do something"])
    with patch("oswald.cli.__main__.run", new_callable=AsyncMock, return_value=0) as execute:
        assert main() == 0
    args = execute.call_args.args[0]
    assert args.plan is None and args.planner is None
    assert args.stage_timeout == 1800


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("saved_plan", [False, True])
async def test_cli_composes_real_agents_and_returns_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    success: bool,
    saved_plan: bool,
) -> None:
    args = argparse.Namespace(
        repo=str(tmp_path),
        output=str(tmp_path / "reports"),
        plan=str(tmp_path / "plan.json") if saved_plan else None,
        planner=None,
        image="python:3.12-slim",
        stage_timeout=60,
        request="Fix value",
        base_ref="HEAD",
    )
    result = RunResult(
        task_id="test",
        status="SUCCEEDED" if success else "STOPPED",
        stage="reviewer",
        reason="result",
        attempts=1,
    )
    with patch("oswald.cli.__main__.Orchestrator") as pipeline:
        pipeline.return_value.run = AsyncMock(return_value=result)
        code = await run(args)
    assert code == (0 if success else 1)
    assert json.loads(capsys.readouterr().out)["status"] == result.status
    factory = pipeline.call_args.args[3]
    runtime = FakeSandboxRuntime(SandboxConfig(task_id="test", workspace_path=tmp_path))
    agents = factory(tmp_path, runtime, FakeWorkspaceDiff())
    if saved_plan:
        assert isinstance(agents.planner, SavedPlanPlanner)
    else:
        assert isinstance(agents.planner, PlannerAdapter)
        assert agents.planner.agent.workspace_root == tmp_path
        assert agents.planner.agent.llm is agents.coder.llm
        assert agents.planner.agent.model
    assert isinstance(agents.coder, CoderAgent)
    assert isinstance(agents.tester, TesterAgent)
    assert isinstance(agents.reviewer, ReviewerAgent)
    assert agents.coder.workspace_root == agents.tester.workspace_root == tmp_path
    assert agents.coder.sandbox is agents.tester.sandbox is runtime


def test_cli_continue_requires_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["oswald", "continue"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


@pytest.mark.parametrize("image", [None, "project:prepared"])
async def test_cli_continue_dispatches_task_id(tmp_path: Path, image: str | None) -> None:
    args = argparse.Namespace(
        repo=str(tmp_path),
        output=str(tmp_path / "reports"),
        plan=None,
        planner=None,
        image=image,
        stage_timeout=60,
        request="continue",
        task_id="abc",
    )
    result = RunResult(task_id="abc", status="SUCCEEDED", stage="reviewer", reason="ok", attempts=1)
    with patch("oswald.cli.__main__.Orchestrator") as pipeline:
        pipeline.return_value.continue_task = AsyncMock(return_value=result)
        assert await run(args) == 0
        if image is None:
            pipeline.return_value.continue_task.assert_awaited_once_with("abc")
        else:
            pipeline.return_value.continue_task.assert_awaited_once_with("abc", image=image)
        pipeline.return_value.run.assert_not_called()
