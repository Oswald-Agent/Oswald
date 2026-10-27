# Running the minimal orchestrator locally

The orchestrator is ordinary async Python. It creates a task ID and an isolated Git
worktree, starts a Docker sandbox, and runs Planner → Coder → Tester → Reviewer.
It saves validated outputs, reports the terminal stage and reason, saves a cumulative
patch, stops the container, and removes the task worktree and branch.

The CLI uses the real Planner, Coder, Tester, and Reviewer implementations by
default. Planner is integrated from upstream prototype PR #21 (`488d630`). A small
adapter maps the task request to Planner's `prompt` field, passes the pinned base
commit as context, and checks that Planner is bound to the task worktree.

The automated flow tests script model responses while exercising all four real
agents, their tools, and the production CLI composition. They verify the integration
without API keys; they do not prove a live model will implement arbitrary requests.

## Verify without an API key

From this branch's checkout, in Bash/WSL:

```bash
uv sync --locked --all-groups
export LITELLM_LOCAL_MODEL_COST_MAP=True
uv run pytest tests/unit/test_orchestrator.py tests/unit/test_orchestrator_cli.py tests/integration/test_orchestrator_flow.py -m 'not integration' -q
```

The environment variable makes LiteLLM use its bundled model-cost data, avoiding a
network fetch during imports. This check uses real Git worktrees and real agent
classes with scripted model responses. Its local test command runs against a small
fixture repository, without Docker or external services.

For the real Docker check, ensure Docker Desktop/Engine is running and your current
shell can run `docker info`, then run:

```bash
docker pull python:3.12-slim
uv run pytest tests/integration/test_orchestrator_flow.py -m integration -v
```

Expect **two Docker cases to pass**, rather than skip: first-attempt success and
one correction. The correction case deliberately implements the wrong answer,
runs a failing `unittest` command inside Docker, sends findings back to Coder, and
passes on attempt two. In both cases, Planner uses its actual list/read tools on
the committed task snapshot, even though the source checkout has a dirty file.
Tests also check that Reviewer sees the test-file diff, the final patch passes
`git apply --check`, and the task branch, worktree, and container are cleaned up.
No model API key is used.

For the repository checks:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy src tests
uv run lint-imports
uv run pytest
```

## Run a task through the CLI with real models

The CLI is `uv run python -m oswald.cli`. The real Planner runs by default;
no saved plan or custom factory is needed.

Create `.env` in this checkout (or export the corresponding environment variables)
with `LLM_PROVIDER_KEY`, `PLANNER_MODEL`, `CODER_MODEL`, `TESTER_MODEL`, and
`REVIEWER_MODEL`. Use your provider's key and supported LiteLLM model identifiers.
A Git worktree does not automatically inherit another checkout's ignored `.env`.

The following Bash example creates a separate demo repository. Keep Oswald as your
current directory so `uv run` uses Oswald's environment:

```bash
demo_dir=$(mktemp -d)
git -C "$demo_dir" init -b main
printf 'def value():\n    return 1\n' > "$demo_dir/app.py"
printf '__pycache__/\n.oswald/\n' > "$demo_dir/.gitignore"
git -C "$demo_dir" add .
git -C "$demo_dir" -c user.name=Demo -c user.email=demo@example.com commit -m 'Demo baseline'
uv run python -m oswald.cli \
  'Make value() in app.py return 2. Verify it with Python unittest, which is available in the sandbox.' \
  --repo "$demo_dir" \
  --image python:3.12-slim \
  --output .oswald/runs
```

This command makes real model API calls. The automated tests do not require model
credentials. For your own project, replace `--repo` with its absolute path and
`--image` with a prebuilt image containing its test dependencies.

`--repo` must point to the Git repository root (containing `.git`), with at least
one commit. An empty subfolder of another repository is not a standalone project.
For a new project, initialize Git in that folder and commit its baseline first.
React/Node projects need a Node image with their npm dependencies cached or installed;
`python:3.12-slim` does not provide Node/npm, and sandbox networking is disabled.

Model responses allow up to 16,384 output tokens by default so larger structured
plans and file edits can complete. For low provider request quotas, set
`LLM_MIN_REQUEST_INTERVAL_SECONDS=15` to space requests, including retries, within
one CLI process. Concurrent runs still share provider quotas. Pacing cannot restore
an exhausted daily quota; enable billing, wait for reset, or select an available
model using the agent model environment variables. Model listings can include
models that reject generation for a particular account.

The runtime binds the isolated worktree at `/workspace`; Planner's read tools,
Coder's edit tools, and Tester's Docker commands all use the same repository
snapshot. Uncommitted changes in the source checkout are excluded. `--base-ref`
selects the commit/branch to snapshot (defaults to `HEAD`).

Exit code `0` means tests passed and Reviewer approved. Exit code `1` means stopped
or failed. Ctrl-C records cancellation and exits `130` after cleanup. JSON output
includes the task ID, stage, reason, attempts, base commit, artifact references, and
patch path. Logs go to stderr.

## Reading agent progress logs

Progress uses the existing scrolling logs on stderr. Stdout remains a single JSON
result, so redirecting it to a file still works. Every agent attempt reports its
identity, elapsed time, confirmed input/output tokens, attempted tool calls, model
calls, and tool errors. For example (illustrative values):

```text
12:00:08Z INFO    CODER    started        elapsed=0.0s tokens=0/0 tool_calls=0 llm_calls=0 attempt=1 task_id=a81c1234
12:00:12Z INFO    CODER    llm_completed  elapsed=4.0s tokens=2300/310 tool_calls=0 llm_calls=1 attempt=1 task_id=a81c1234 turn=1 model="provider/model"
12:00:12Z INFO    CODER    tool_started   elapsed=4.0s tokens=2300/310 tool_calls=1 llm_calls=1 attempt=1 task_id=a81c1234 tool="read_file_range"
12:00:22Z INFO    CODER    running        elapsed=14.0s tokens=2300/310 tool_calls=1 llm_calls=2 attempt=1 task_id=a81c1234 action="waiting_for_model"
12:00:39Z INFO    CODER    completed      elapsed=31.0s tokens=4800/810 tool_calls=3 llm_calls=3 attempt=1 task_id=a81c1234 files_changed=2
```

`tokens=input/output` is cumulative within the current agent attempt, rather than
just the latest model request. Counts update after a response returns; a pending
request shows the last confirmed count. If any request fails, is cancelled, or
omits usage, the attempt's token totals become `unknown` (`null` in JSON), rather
than claiming complete accounting. Provider-reported zero usage remains zero.
These are response usage counts, not billing estimates; unavailable usage from
provider-internal retries cannot be reconstructed.

`tool_calls` counts actual dispatch attempts, including rejected/failed tools.
Model-proposed calls that are never dispatched are not counted. `tool_errors`
counts dispatches the agent reports as unsuccessful, including aborted calls;
a test command that finds failing tests may therefore also be an unsuccessful
tool invocation. Reviewer normally has zero tool calls. A saved plan has no model
usage. A custom Planner's internal calls are metered only if it uses the shared
`complete` and `tool_call` helpers from `oswald.core.agents.telemetry`.

A quiet agent emits `running` approximately every 10 seconds with its current
action, such as `waiting_for_model` or `tool:run_test_command`. Other progress
messages suppress redundant heartbeat messages. This is an async timer, so a
synchronous tool that blocks the event loop can delay a heartbeat. Agent elapsed
time includes its tools and model waits; run elapsed time also includes workspace
setup and cleanup. Timers use a monotonic clock. Log timestamps use UTC (`Z`).

Use `--progress-interval 2` for a shorter quiet-period interval, or `--verbose`
for provider request/retry details and successful tool completion events. The
interval must be finite and positive. Counters reset for each correction attempt;
run totals accumulate across all attempts. Existing warnings and provider retry
logs include the active task, agent, attempt, and current counters.

The final `run.completed` line reports status, run totals, the latest available
test counts and review verdict, and the patch/report paths. Failed/stopped runs
also include a short reason. Zero counted tests are explicitly labelled
`No tests reported` in the Tester log; the logger does not invent collection
results or alter test routing. Terminal fields are shortened for readability;
the structured log preserves full task IDs and UTC timestamps.

The CLI also streams JSONL progress events to `<output>/progress.jsonl`, shared
across runs in that output directory. Rotation keeps the current file and two
backups (`progress.jsonl.1` and `.2`), targeting 1 MiB per file. Rotation bounds
history, not a single exceptionally large record; full task artifacts and sandbox
command logs have their own existing retention behavior. Use separate output
directories for concurrently running CLI processes, since file rotation is
process-local. Per-run final metrics, including one small summary for each agent
attempt, remain in `<output>/<task-id>/run.json` under `metrics` and in stdout JSON.

The default event sink discards raw lifecycle/tool event objects after use. Only
small counters, the current action, and the bounded pipeline's attempt summaries
are retained for progress. Tests or integrations that need raw events can still
explicitly inject `InMemoryEventSink`. Progress requires no extra model calls.
Programmatic callers retain their logging configuration; to use the CLI formatting
and bounded JSONL output, wrap execution in
`with oswald.observability.logging.configure_logging(output_dir): ...`.

## Saved outputs

Inspect `.oswald/runs/<task-id>/`:

- `request.json`: original request, workspace, and pinned base commit.
- `attempt-0/spec.json`: validated Planner output.
- `attempt-1/` and optionally `attempt-2/`: Coder, Tester, cumulative review input,
  and Reviewer JSON reports. Their `content` fields contain the Markdown reports.
- `spec/`, `code_changes/`, `test_results/`, `review_changes/`, `review/`: existing
  content-addressed artifact storage with lineage. Identical payloads are deduplicated.
- `logs/`: copied Docker command stdout/stderr logs.
- `final.patch`: cumulative implementation and test changes, also saved on failures.
- `run.json`: terminal outcome, cleanup errors, and aggregate/per-attempt `metrics`.

Review the patch before applying it to the original repository:

```bash
git -C "$demo_dir" apply --check /absolute/path/to/final.patch
git -C "$demo_dir" apply /absolute/path/to/final.patch
```

The orchestrator does not commit, push, merge, or publish changes. Approval here
means the automated Reviewer approved; it does not substitute for human approval.
If patch/log saving or container teardown fails, the result records the failure
and retains the worktree for recovery. Do not remove it until you recover the changes.

## Continue a task

Resume a failed, cancelled (Ctrl-C), or stopped run with its printed task ID:

```bash
uv run python -m oswald.cli continue <task-id>
```

Use the same `--output` directory (default `.oswald/runs`). New runs remember
the source repository and sandbox image automatically. For older runs, supply
the original `--repo` and `--image`. An explicit `--image` overrides the saved image
when resuming, for example after preparing missing dependencies on the host.
The command restores the saved patch on the original base
commit and keeps the same task ID. A failed Tester resumes at Tester; a failed
Reviewer reuses the saved passing/failing test report and resumes at Reviewer.
Completed Planner and Coder outputs are reused. The unfinished agent starts a new
agent call; its internal conversation/tool loop is not resumed.

After the correction limit or a Reviewer rejection, continuing sends the saved
findings to Coder for another bounded invocation (at most two Coder attempts).
Attempt numbers continue increasing. A Coder escalation retries Coder; planning
questions retry Planner. Successful runs return the saved result without agent
calls. Earlier outcomes are preserved as `run-<id>.json`, and artifact versions
remain in the store.

This uses the saved `run.json`, `request.json`, artifacts, and `final.patch`, so it
also supports runs produced before this command existed. Abrupt process kills
without a saved terminal report are unsupported. Runs with cleanup errors require
recovery of their retained workspace first. Do not continue a task while another
invocation of that task is running.

## Programmatic use and optional Planner overrides

For programmatic use, inject an `Agents` factory and a `SandboxRuntime` factory into
`Orchestrator`, then `await orchestrator.run(request, base_ref="HEAD")` or
`await orchestrator.continue_task(task_id)`. Wrap a
`PlannerAgent` with `PlannerAdapter`. Import the orchestration types from
`oswald.core.orchestrator` and `PlannerAgent` from `oswald.core.agents`. Construct
`PlannerAgent` with the `root` path supplied
to your agent factory, as the default CLI does.

`PlannerInput` from `oswald.core.orchestrator` contains `task_id`, `request`,
`workspace_root`, and `base_commit`. `PlannerAdapter` translates it to the agent's
`PlannerInput` (`task_id`, `prompt`, and `context`). All four CLI agents use
`Settings` for model configuration. No API server, database, Redis, Taskiq, or
orchestration framework is needed.

The CLI retains two optional overrides for diagnostics or custom integrations:

- `--plan /absolute/path/to/plan.json` uses a saved plan instead of calling Planner.
  The file contains `{"base_commit": "<exact commit SHA>", "spec": {<PlanSpec fields>}}`.
  A mismatched commit is rejected; the adapter assigns the new task ID to the plan.
- `--planner module:factory` loads a synchronous factory receiving the isolated
  workspace `Path`. Return an object with
  `async run(data: oswald.core.orchestrator.PlannerInput) -> PlanSpec`.

These overrides are mutually exclusive and optional. The orchestrator validates
and persists the returned specification regardless of which Planner is used.

## Routing and limits

| Result | Action |
| --- | --- |
| Planning questions in either control field | Stop at Planner before Coder |
| Coder escalation | Stop at Coder |
| Agent exception, timeout, invalid output, foreign task ID, persistence or sandbox error | Fail with stage and reason |
| Tester infrastructure/agent failure | Fail at Tester; no correction |
| Reviewer `REJECTED` | Stop at Reviewer; no correction |
| Failed tests or Reviewer `CHANGES_REQUESTED` | Send findings to Coder once, then rerun Tester and Reviewer |
| Second unsuccessful attempt | Stop this invocation; `continue` can start another bounded invocation |
| Passing tests plus Reviewer `APPROVED` with no blockers | Succeed |

Reviewer runs after normal test failures to supply additional findings. A rejection
still ends the run. The per-stage timeout defaults to 1800 seconds (30 minutes) and is configurable
with `--stage-timeout`; agent turn ceilings remain in effect. Each model request
has a 300-second deadline (5 minutes) and one transient retry by default, configured with
`LLM_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES` in `.env`. SDK-internal retries are
disabled so the adapter owns the retry limit. A stalled request emits a start log
and either retries or produces a saved stage failure, within the configured deadlines.

Tester uses `run_diagnostic_command` for runtime probes. Diagnostics and unavailable
optional runners do not count as tests or poison a subsequent valid suite run. Git
inspection uses the supplied host diff; task Git metadata is not mounted into the
container. For the standalone demo, Tester uses standard-library unittest. It parses
unittest's real test counts and rejects zero-test discovery. Failed discovery
attempts are diagnostic evidence, so correcting the directory and running a real
suite can recover without stale failures. Inline Python probes also remain diagnostics. Existing projects still
need an image containing their actual framework and dependencies.

Docker starts a task container using an already running Docker daemon. It does not
start the daemon or install your project dependencies. The default image supports
the standard-library demo above. For a real project, use `--image` with a prebuilt
image containing its test tools and dependencies: the sandbox has networking disabled
and a read-only root filesystem. Native Windows execution has not been verified;
the documented commands and integration checks target Bash/WSL/Linux.

DeepSeek V4/Flash requests explicitly disable extended thinking. The provider's
[thinking-mode contract](https://api-docs.deepseek.com/guides/thinking_mode/) requires
reasoning replay for thinking tool conversations; this minimal pipeline uses ordinary
content/tool messages and a bounded output budget. Other providers keep their current
request mode.

Shared plans can contain production and test tasks. Coder's assignment lists only
production tasks; Tester handles test tasks. Reviewer receives the cumulative diff
after Tester, so its instructions distinguish Tester additions from Coder's earlier
source-only notes and whitelist.
