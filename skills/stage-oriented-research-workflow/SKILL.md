---
name: stage-oriented-research-workflow
description: Run bounded, user-gated research work as explicit Stages with local evidence, optional NORMAL/FRESH ChatGPT review, and fail-closed approvals. Do not use for ordinary one-off coding or autonomous loops.
metadata:
  short-description: User-gated Stage workflow for research tasks
---

# Stage-oriented research workflow

Use this skill when a research or engineering effort benefits from a stable,
user-visible Stage goal, bounded local evidence, optional technical review, and
an explicit human decision at closeout. Keep the workflow project-agnostic:
read the target repository's own `.research/PROJECT.md` and Stage contract; do
not invent domain-specific methods or copy a previous project's assumptions.

## Operating contract

- A Stage contract is user-authored and starts as `PLANNED`. Verify its goal,
  protected paths, allowed paths, checks, baseline, and review artifacts before
  doing work.
- `prepare` registers a contract. Only an explicit user action may `start` a
  Stage. Work remains inside the contract's allowed scope and produces local,
  reviewable evidence.
- Codex decides whether a new local evidence revision warrants a consultation;
  a consultation is never an automatic loop and never executes GPT's advice
  without local validation.
- Use NORMAL review for a new, bounded continuation of the same Stage. Reuse
  the existing conversation only when the Stage is active and evidence is new.
- Use FRESH review for a closeout or an explicit architecture/representation
  challenge. It must contain only the current bounded context and must not carry
  a previous recommendation or sunk-cost narrative.
- `STAGE_READY` means the declared checks and review artifacts are present and
  the Stage is ready for user inspection; it is not scientific PASS and is not
  approval. Stop and wait for the user.
- `approve` records the user's acceptance. `reject` returns the same Stage to
  `ACTIVE` with feedback. `stop`/`pause` closes it. A later Stage remains
  `PLANNED` until the user explicitly starts it.

## Safe execution

Fail closed when a contract, state, digest, manifest, evidence path, response,
or required artifact is missing, ambiguous, duplicated, or inconsistent. Do
not widen paths, expose secrets, modify browser profiles, bypass a Gate, or
retry a failed bridge request implicitly. Prefer a simpler representation,
direct measurement, physical/real-world constraints, and scale separation;
reconsider a route when repeated same-abstraction work does not improve the
user-visible result. Keep user-visible artifacts and checks understandable.

## Thin CLI entry point

The reference local CLI is `scripts/stage_cli.py` in the supervisor repository.
When a user starts with a rough project requirement, run the independent Step
11 Requirements Intake first (`scripts/requirements_cli.py` or the equivalent
`requirements-*` commands in `stage_cli.py`). It must reach an explicit brief
approval or cancellation before a Stage contract is prepared.
From a project repository, use the equivalent commands below (pass `--repo`
when the current directory is not the project):

```text
python <workflow-root>/scripts/stage_cli.py prepare --contract .research/stages/stage-001/contract.json
python <workflow-root>/scripts/stage_cli.py start stage-001
python <workflow-root>/scripts/stage_cli.py show stage-001
python <workflow-root>/scripts/stage_cli.py consult stage-001 --evidence .research/latest-result.txt
python <workflow-root>/scripts/stage_cli.py fresh-review stage-001 --reason "closeout review"
python <workflow-root>/scripts/stage_cli.py show-artifacts stage-001
python <workflow-root>/scripts/stage_cli.py approve stage-001
```

The CLI persists bounded Stage state under `.research/stage-state.json` and
consultation metadata under `.research/reviews/index.json`. It prints a live
response to the caller when a bridge review completes, but does not persist the
prompt or raw response. `--dry-run` validates a consultation plan without
contacting ChatGPT. The headed bridge may take several minutes; do not solve a
timeout or rate limit with rapid retries.

## Step 12 context compaction and retention

After Step 11 reaches `APPROVED`, the independent local helpers in
`src/project_context.py` may create exactly one canonical
`.research/PROJECT_CONTEXT.md`. `DRAFT`, `WAITING_USER_APPROVAL`, and
`CANCELLED` briefs fail closed. The context is a deterministic, bounded
handoff containing the real target, inputs/outputs, user-visible success,
constraints, non-goals, preferences, decisions, status, selected/rejected
routes (with short evidence references), unresolved questions, next action,
artifact references, and Git identity. It never stores a transcript or raw
browser/GPT material and uses at most nine selected resources.

`src/artifact_retention.py` records `CANONICAL`, `MILESTONE_EVIDENCE`,
`ACTIVE_REFERENCE`, and workflow-owned `EPHEMERAL` paths in
`.research/ARTIFACT_RETENTION_MANIFEST.json`. Cleanup is allowed only for
workflow-owned ephemeral paths; source, data, business files, Git history,
canonical files, and milestone evidence are protected. Re-running compaction
is idempotent and does not create versioned sibling context files.

Step 12 deliberately does not call the bridge or ChatGPT. Step 13 is a
separate, explicit Project Discovery operation in `src/project_discovery.py`:
it requires an approved brief, a sanitized `chatgpt_project_url` (or
`chatgpt_project_binding.url`), one injected `fresh` consultant call, and
local repository verification before promoting any candidate. It writes only
the bounded canonical `.research/discovery/DISCOVERY_REPORT.json`; duplicate
evidence, retries, Stage starts, production architecture selection, and
business/source writes are blocked. Use `python scripts/step13_acceptance.py`
for its fake-consultant offline gate.

Step 14 consumes an approved brief/context/discovery report and first builds a
compact `CODEX_FEASIBILITY` packet. It then uses one injected FRESH consultant
to derive exactly one primary route and at most four alternatives, excluding
prior recommendation/sunk-cost material from the prompt. It writes only the
atomic `.research/blueprint/PROJECT_BLUEPRINT.md` and
`BLUEPRINT_MANIFEST.json`, returns `PROJECT_BLUEPRINT_READY` /
`READY_FOR_STAGE_PLANNING`, and never creates or starts a Stage. Use
`python scripts/step14_acceptance.py` for the Step 14 offline gate, then use
`python scripts/stage_cli.py --repo PATH plan-stage` to derive and register one
`PLANNED` Stage from the verified Bootstrap outputs. Step 15 never starts the
Stage; its offline gate is `python scripts/step15_acceptance.py`.
