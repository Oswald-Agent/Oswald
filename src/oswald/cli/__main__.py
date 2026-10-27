"""Run with python -m oswald.cli; no database, Redis, or worker required."""

import argparse
import asyncio
import importlib
import json
import math
import sys
from pathlib import Path
from typing import cast

from oswald.config import Settings
from oswald.core.agents.coder import CoderAgent
from oswald.core.agents.planner import PlannerAgent
from oswald.core.agents.reviewer import ReviewerAgent
from oswald.core.agents.tester import TesterAgent
from oswald.core.artifacts.models import PlanSpec
from oswald.core.artifacts.storage import FileSystemArtifactStore
from oswald.core.orchestrator.pipeline import Agent, Agents, Orchestrator, PlannerInput
from oswald.core.orchestrator.planner import PlannerAdapter
from oswald.core.sandbox import DockerSandboxRuntime
from oswald.core.sandbox.protocol import SandboxRuntime
from oswald.core.vcs.workspace import WorkspaceDiff
from oswald.observability.logging import configure_logging


class SavedPlanPlanner:
    """Optional adapter for a manually prepared plan bound to an exact commit.

    The default CLI uses PlannerAgent. A saved plan supports replay and diagnostics.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    async def run(self, data: PlannerInput) -> PlanSpec:
        raw = json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
        if raw["base_commit"] != data.base_commit:
            raise ValueError("Saved plan base_commit differs from the isolated repository snapshot")
        spec = PlanSpec.model_validate(raw["spec"])
        return PlanSpec.model_validate({**spec.model_dump(), "task_id": data.task_id})


async def run(args: argparse.Namespace) -> int:
    from oswald.core.llm.adapters.litellm_adapter import LiteLLMAdapter, RetryConfig

    settings = Settings()
    client = LiteLLMAdapter(
        api_key=settings.llm_provider_key,
        api_base=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        min_request_interval_seconds=settings.llm_min_request_interval_seconds,
        retry_config=RetryConfig(max_retries=settings.llm_max_retries),
    )

    def agents(root: Path, sandbox: SandboxRuntime, diff: WorkspaceDiff) -> Agents:
        if args.plan:
            planner: Agent[PlannerInput, PlanSpec] = SavedPlanPlanner(Path(args.plan).resolve())
        elif args.planner:
            module, separator, name = args.planner.partition(":")
            if not separator or not module or not name:
                raise ValueError("--planner must be module:factory")
            planner = cast(
                Agent[PlannerInput, PlanSpec], getattr(importlib.import_module(module), name)(root)
            )
        else:
            planner = PlannerAdapter(PlannerAgent(client, root, model=settings.planner_model))
        return Agents(
            planner=planner,
            coder=CoderAgent(
                client, sandbox, diff, root, model=settings.coder_model, max_turns=100
            ),
            tester=TesterAgent(client, sandbox, root, model=settings.tester_model, max_turns=100),
            reviewer=ReviewerAgent(client, model=settings.reviewer_model),
        )

    output_dir = await asyncio.to_thread(Path(args.output).resolve)
    orchestrator = Orchestrator(
        Path(args.repo),
        output_dir,
        FileSystemArtifactStore(output_dir),
        agents,
        DockerSandboxRuntime,
        image=args.image or "python:3.12-slim",
        stage_timeout=args.stage_timeout,
        progress_interval=getattr(args, "progress_interval", 10),
    )
    if getattr(args, "task_id", None):
        if args.image is not None:
            result = await orchestrator.continue_task(args.task_id, image=args.image)
        else:
            result = await orchestrator.continue_task(args.task_id)
    else:
        result = await orchestrator.run(args.request, base_ref=args.base_ref)
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "SUCCEEDED" else 1


def _progress_interval(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("Progress interval must be finite and positive")
    return interval


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the bounded Planner → Coder → Tester → Reviewer pipeline"
    )
    parser.add_argument("request", help="Task request, or continue to resume a saved task")
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--base-ref", default="HEAD", help="Committed snapshot; uncommitted changes are excluded"
    )
    parser.add_argument("--output", default=".oswald/runs")
    parser.add_argument(
        "--image",
        default=None,
        help="Prebuilt dependency image; new runs default to python:3.12-slim, continue reuses the saved image",
    )
    parser.add_argument("--stage-timeout", type=float, default=1800)
    parser.add_argument(
        "--progress-interval",
        type=_progress_interval,
        default=10,
        help="Seconds between progress logs during quiet agent operations (default: 10)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Include provider request debug logs"
    )
    planner = parser.add_mutually_exclusive_group()
    planner.add_argument(
        "--plan",
        help="Use a saved plan JSON with base_commit and spec instead of the default Planner",
    )
    planner.add_argument(
        "--planner",
        help="Override Planner with module:function; factory receives isolated workspace Path",
    )
    # Keep the existing request syntax; only the reserved "continue" command adds an ID.
    parser.add_argument("task_id", nargs="?", help="Saved task ID for the continue command")
    args = parser.parse_args()
    if args.request == "continue":
        if not args.task_id:
            parser.error("continue requires a task ID")
    elif args.task_id:
        parser.error("a task ID is only accepted after continue")
    try:
        with configure_logging(Path(args.output), verbose=args.verbose):
            return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
