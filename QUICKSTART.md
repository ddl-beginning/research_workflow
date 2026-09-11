# Quickstart: adopt a bounded Stage workflow

This workflow adds only project-local `.research/` state. It does not modify
the target project's source tree, browser profile, global Codex settings, API
keys, or tunnel configuration.

## 0. Start with Step 11 Requirements Intake

Before creating a Stage contract, initialize the project-level canonical brief.
The intake is local and deterministic; it does not run Project Discovery or
contact GPT. A natural-language request such as `想做成这个工作流` enters the
interview with one core question:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo PATH requirements-init --rough-requirement "想做成这个工作流"
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo PATH requirements-show
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo PATH requirements-answer --answer "希望用户看到可验证的项目结果"
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo PATH requirements-answer --answer "验收条件是检查可复现"
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo PATH requirements-approve --rationale "用户确认"
```

直接提供完整 brief 时使用 `USER_CONFIRMED_BRIEF`：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo PATH requirements-init --mode USER_CONFIRMED_BRIEF --brief '{"goal":"明确目标","success_criteria":["结果可验证"],"constraints":["不修改业务源代码"]}'
```

唯一持久化文件是 `PATH/.research/PROJECT_BRIEF.json`。brief 的状态为
`DRAFT`、`WAITING_USER_APPROVAL`、`APPROVED` 或 `CANCELLED`；修改始终复用同一
`project_id`。取消后不能通过新命令恢复该 identity。approve 只记录用户决定，
不会隐式准备 Stage、运行 discovery 或发起 ChatGPT 请求。

## 1. Prepare a project-local contract

Copy the structure under `examples/project-adoption/` into a disposable or
ordinary Git repository:

```text
.research/
  PROJECT.md
  stages/
    stage-001/
      contract.json
      artifacts/
      reviews/
```

Edit `PROJECT.md` and `contract.json` so that the goals, inputs, protected and
allowed paths, baseline, required checks, and review artifact requirements are
real for that repository. Keep `status` as `PLANNED`. Never put credentials,
cookies, tokens, browser profile contents, or raw prompts in these files.

## 2. Register and start

From the target repository, run:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py prepare --contract .research/stages/stage-001/contract.json
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py start stage-001
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py show stage-001
```

`prepare` validates the contract and writes `.research/stage-state.json`.
`start` is the only operation that changes `PLANNED` to `ACTIVE`; no command
starts a Stage implicitly. Use `--repo PATH` when running the CLI elsewhere.

## 3. Work inside one Stage

Codex performs local work within `allowed_paths`, observes `protected_paths`,
and records new evidence through the existing supervisor APIs. Keep the
baseline immutable and make checks/artifacts user-visible. The CLI does not
run an executor loop or automatically apply a GPT recommendation.

When a new local evidence revision needs technical review, request one bounded
NORMAL consultation. Evidence paths are repository-relative and may be
repeated only when each path is new to the digest:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py consult stage-001 --evidence .research/latest-result.txt
```

The CLI builds the bounded context from the contract and latest local result,
uses the existing bridge, and reuses the prior NORMAL conversation for the
same Stage when available. A single invocation has `request_count=1`; a bridge
failure is recorded and is never retried implicitly. The headed ChatGPT UI may
take several minutes. `--dry-run` checks the plan without contacting ChatGPT.

Use FRESH only for a closeout or a real architecture/representation review:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py fresh-review stage-001 --reason "closeout review"
```

FRESH uses a new conversation and excludes prior recommendation content. The
policy decision to request FRESH remains explicit; the CLI does not invent a
trigger or loop until one appears.

## 4. Closeout and human Gate

After required checks and review artifacts are present, the controller may
enter `STAGE_READY`. Inspect the result and artifacts:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py show stage-001
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py show-artifacts stage-001
```

`STAGE_READY` is a candidate for user inspection, not scientific PASS and not
approval. The user chooses one explicit action:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py approve stage-001 --rationale "reviewed candidate"
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py reject stage-001 --rationale "need a stronger hard-case artifact"
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py stop stage-001 --rationale "stop this line of work"
```

`reject` returns the same Stage to `ACTIVE`; it never creates a new Stage.
`stop` (also named `pause`) closes the Stage and rejects further executor or
consultation requests. Registering a later Stage leaves it `PLANNED`; the user
must explicitly start it.

## 5. Troubleshooting

- `STATE_NOT_FOUND`: run `prepare` in the target repository.
- `PATH_OUTSIDE_REPOSITORY` or `PROTECTED_PATH_REJECTED`: use a repository-
  relative path that is not protected; do not widen the contract to make a
  convenience path work.
- `STAGE_NOT_ACTIVE` or `*_REJECTED`: inspect `show`; the lifecycle transition
  is not legal in the current state.
- `CONSULTATION_REJECTED`: the evidence digest was already consumed or a
  human/stop Gate is pending; produce new local evidence and inspect the
  failure receipt before doing anything else.
- `BRIDGE_*`: inspect the sanitized bridge receipt. Do not retry rapidly or
  copy browser state into project files. A missing login/profile or UI rate
  limit is a fail-closed external blocker.

## 6. Step 12 context compaction and retention

Once `PROJECT_BRIEF.json` is explicitly `APPROVED`, build the one canonical
context file with the local helper. The helper does not scan the repository or
contact ChatGPT:

```text
python -c "from src.project_context import build_project_context; print(build_project_context(r'PATH'))"
```

Callers may pass explicit `resources` (at most nine) and bounded hand-off
fields such as `target`, `inputs`, `outputs`, `user_visible_success`,
`constraints`, `non_goals`, `preferences`, `decisions`, `status`, routes,
unresolved questions, `next_action`, `artifact_refs`, and `git_identity`.
Repeated calls produce the same semantic digest and keep only
`.research/PROJECT_CONTEXT.md` plus the retention manifest. No transcript,
prompt, browser state, or raw response is copied.

`src/artifact_retention.py` permits cleanup only under workflow-owned
`.research/ephemeral`, `.research/staging`, `.research/tmp`, or the other
explicit workflow staging directories. Source/data, business files, Git
history, canonical files, and milestone evidence are never cleanup targets.
Run the offline gate from this repository:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step12_acceptance.py
```

Step 12 does not call the bridge or ChatGPT. Step 13 is now a separate,
explicitly invoked Project Discovery layer; Step 14 consumes its report and
Step 15 turns the verified Bootstrap hand-off into one `PLANNED` Stage.

## 7. Step 13 bounded Project Discovery

After the brief is `APPROVED` (and, when present, `PROJECT_CONTEXT.md` passes
its Step 12 verification), supply a one-shot consultant and local verifier to
`src.project_discovery.discover_project`. The consultant receives a bounded
`fresh` request containing the Anti-Tunnel rule and the approved Project URL;
there is no default network transport. The canonical result is written to
`.research/discovery/DISCOVERY_REPORT.json` with one primary and at most four
alternatives. A duplicate evidence digest is blocked, repository claims are
not promoted without local verification, and a no-direct-match result is
valid.

Run the entirely offline acceptance gate:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step13_acceptance.py
```

It prints `GPT_PROJECT_DISCOVERY_PASS` on success and performs no real GPT
request.

## 8. Step 14 project blueprint

After Step 13 completes, call `src.project_blueprint.build_project_blueprint`
with an explicitly injected consultant. The helper first compacts the
`CODEX_FEASIBILITY` packet, then makes one bounded `fresh` review using the
approved Project URL. It derives exactly one primary route and at most four
alternatives without carrying the previous recommendation or sunk-cost route.
The canonical outputs are:

```text
.research/blueprint/PROJECT_BLUEPRINT.md
.research/blueprint/BLUEPRINT_MANIFEST.json
```

The result is `PROJECT_BLUEPRINT_READY` with next action
`READY_FOR_STAGE_PLANNING`; it does not create/start a Stage. Same-input
re-runs reuse the canonical files without another consultation. Run the
offline gate with:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step14_acceptance.py
```

Success marker: `GPT_CODEX_BLUEPRINT_REVIEW_PASS`.

## 9. Step 15 Bootstrap-to-Stage planning

After the brief, context, Discovery report, Blueprint, and
`.research/bootstrap_state.json` all pass their canonical checks, derive and
register one Stage with the existing controller:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py --repo PATH plan-stage
```

The planner writes the bounded contract to
`.research/stages/<stage-id>/contract.json`, persists the controller snapshot
at `.research/stage-state.json`, and writes the reviewable planning envelope
to `.research/stage-planning/STAGE_PLAN.json`. The result carries the
`STAGE_PLANNING_PASS` marker and leaves the Stage `PLANNED`; it does not call
ChatGPT or move the Stage to `ACTIVE`. Use `start <stage-id>` only after the
user has reviewed the generated contract. Re-running against unchanged
Bootstrap inputs is idempotent, while changed evidence is rejected until the
planned artifact is explicitly dealt with.

Run the offline gate with:

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step15_acceptance.py
```

To consume an already completed Bootstrap checkout, pass `--repo PATH` to
the acceptance script; missing or stale Bootstrap artifacts fail closed and
are never fabricated.
