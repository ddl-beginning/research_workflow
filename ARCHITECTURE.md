# Architecture

The workflow is a small, local, finite system. It has no web frontend, daemon,
dashboard, recursive agent, automatic cross-Stage execution, or production
promotion path.

```text
User
  │ rough requirement or USER_CONFIRMED_BRIEF
  ▼
Requirements Intake ── one question at a time ──► .research/PROJECT_BRIEF.json
  │ explicit brief approval (no downstream trigger)
  ▼
Codex local work ── new evidence/state revision ──► Stage Controller
                                                     │ bounded Gate/state
                                                     ▼
                                          context + artifact layer
                                                     │
                                                     ▼
                              existing consult_gpt browser bridge
                                                     │ one request
                                                     ▼
                                           ChatGPT Pro headed UI
```

## Responsibilities

| Layer | Owns | Does not own |
| --- | --- | --- |
| User | Stage goals, value judgments, start and final Gate decisions | Browser transport or machine context copying |
| Requirements Intake | Project identity, canonical brief, one-question interview, brief approval/cancellation | Project Discovery, Stage creation/start, GPT calls, repository mutation |
| Codex | Scoped local implementation, checks, evidence, and interpreting bounded response | Bypassing state or endlessly retrying GPT |
| Stage Controller | Contract validation, baseline identity, finite lifecycle, iteration/request gates, human review | Research planning, browser calls, automatic workers |
| Context/artifact layer | Bounded evidence, hashes, manifests, review references, NORMAL reuse and FRESH isolation | Whole-repository scanning or route selection |
| Dialogue policy | Consultation trigger/mode and minimal decision semantics | File upload, executor actions, state transitions |
| Browser bridge | Headed UI upload, one request, response and sanitized receipt | Recursive calls, secrets, or interpretation |
| ChatGPT Pro | Technical/research analysis and scoped recommendation | Editing the repository or starting another Stage |

## Lifecycle

```text
PLANNED --(user start)--> ACTIVE
ACTIVE --(checks + artifacts + FRESH closeout)--> STAGE_READY
STAGE_READY --(user approve)--> APPROVED
STAGE_READY --(user reject)--> ACTIVE
ACTIVE/STAGE_READY --(user stop)--> STOPPED
```

`BLOCKED` is a fail-closed terminal state for missing capability or blocked
execution. Planner decisions such as `CONTINUE`, `REPLAN`, and `HUMAN_GATE` are
bounded decision data; they do not create an automatic loop or silently change
the Stage goal.

NORMAL continuation reuses a conversation only after a new local evidence
digest and while the Stage is `ACTIVE`. FRESH creates an isolated conversation
for closeout or a major representation/architecture check and excludes prior
recommendations. A bridge invocation must have exactly one request. Browser
failure, rate limiting, malformed response, duplicate digest, secret detection,
path escape, or inconsistent hash/manifest stops the operation.

## Product surface

`scripts/stage_cli.py` is intentionally a thin adapter over the existing
controller. It persists `.research/stage-state.json` and bounded consultation
metadata in `.research/reviews/index.json`. It does not persist prompts, raw
GPT responses, cookies, tokens, DOM dumps, or browser profile contents. It
prints a completed response to the caller for immediate Codex interpretation;
the review index stores only IDs, mode, conversation identity, counts, hashes,
receipt reference, and elapsed time.

## Requirements Intake boundary

`src/project_intake.py` and `src/project_state.py` are independent of
`stage_controller.py` and `stage_integration.py`. They persist only
`.research/PROJECT_BRIEF.json` (a short Markdown rendering is optional, but
this PoC does not create one). The canonical states are:

```text
DRAFT --(one user answer at a time)--> WAITING_USER_APPROVAL
  │                                      │
  └── cancel (terminal) ──> CANCELLED   └── explicit approve ──> APPROVED
```

`USER_CONFIRMED_BRIEF` and `CODEX_REQUIREMENTS_INTERVIEW` are input modes, not
additional lifecycle states. The interview asks only user-judgement questions
about the desired outcome and acceptance; local repository facts are collected
read-only. A revision keeps the same path-derived `project_id`; revising an
approved brief reopens the approval gate. Approval has no discovery, Stage, or
GPT side effect, and cancellation refuses future resume/update operations.
The intake module carries a code-level `gpt_calls == 0` guard and never imports
the headed browser bridge.

## Step 12 context boundary

Step 12 is a local, independent hand-off layer after brief approval:

```text
APPROVED PROJECT_BRIEF.json
          │ explicit selected fields/resources (<= 9)
          ▼
PROJECT_CONTEXT.md + ARTIFACT_RETENTION_MANIFEST.json
```

`src/project_context.py` never performs discovery, route discovery, stage
transitions, or bridge/ChatGPT calls. It retains only the explicit project
target, inputs/outputs, user-visible success, constraints, non-goals,
preferences, decisions/status, selected and rejected routes with short reason
and evidence reference, unresolved questions, next action, artifact refs, and
Git identity. It rejects transcript-shaped fields, secrets, unsafe paths, and
unbounded resources. The canonical context and retention manifest are written
atomically, with deterministic digests and no versioned sibling files.

`src/artifact_retention.py` distinguishes `CANONICAL`, `MILESTONE_EVIDENCE`,
`ACTIVE_REFERENCE`, and workflow-owned `EPHEMERAL`. Only the last class may be
cleaned, and only under known `.research` workflow directories. Business/source
files, data, Git history, milestone evidence, and canonical files remain
untouched. Step 12 itself does not start discovery; its local gate is
`scripts/step12_acceptance.py`.

## Step 13 Project Discovery boundary

```text
APPROVED PROJECT_BRIEF.json + verified PROJECT_CONTEXT.md (if present)
          │ canonical AVAILABLE_ASSETS.json (metadata only)
          │ LOCAL_PROJECT_PROFILE.md when a local project exists
          │ bounded evidence packet + Anti-Tunnel rule
          ▼
  injected FRESH consultant (exactly one request)
          │ candidate claims
          ▼
  injected/local repository verifier ──► DISCOVERY_REPORT.json
```

`src/project_discovery.py` requires an approved brief, extracts and sanitizes
`chatgpt_project_url` or `chatgpt_project_binding.url`, and refuses to proceed
without that binding. It accepts only an explicitly injected consultant for
the request; the offline gate uses a fake and never calls the browser bridge.
The response is bounded to one primary recommendation and at most four
alternatives. Repository URLs are canonicalized and verified locally before
being promoted. The canonical report is atomically written under
`.research/discovery/`; duplicate evidence digests and retries after a failed
consultation are blocked. No Stage is started, no production architecture is
selected, and no business/source file is changed. `no_direct_match_found` is a
valid report state.

`src/asset_layer.py` owns the single `available_assets.v1` inventory. It
performs a read-only local audit, exposes an existing repository as
`CANDIDATE_USER_PROJECT` (Candidate 0), and records verified external
repositories under the same `candidate_evidence.v1` facts boundary. Candidate
assets remain `candidate_only`; neither local nor external provenance is an
automatic preference. Discovery and Blueprint receive a bounded Asset Pack
with canonical metadata and `whole_repo_attached=false`.

## Step 14 Project Blueprint boundary

```text
verified PROJECT_BRIEF + PROJECT_CONTEXT + DISCOVERY_REPORT
          │ compact CODEX_FEASIBILITY (no prior route fields)
          ▼
  injected FRESH consultant (exactly one request)
          │ one primary route + <= 4 alternatives
          ▼
  PROJECT_BLUEPRINT.md + BLUEPRINT_MANIFEST.json
```

`src/project_blueprint.py` requires all three canonical preconditions, passes
the strict approved Project URL to the injected consultant, and excludes
previous recommendation/sunk-cost route material from the FRESH prompt. The
result is `PROJECT_BLUEPRINT_READY` with
`READY_FOR_STAGE_PLANNING`; files are atomic and same-input runs are
idempotent. The route carries one explicit composition decision (`KEEP_EXISTING`,
`KEEP_AND_OPTIMIZE`, `KEEP_AND_BORROW`, `REPLACE_BASE`,
`BUILD_FROM_EXISTING_LIBRARIES`, or `BUILD_NEW`) plus keep/borrow/replace/
retire/new-component fields. It never creates or starts a Stage and never
changes business or source files. Use `scripts/step14_acceptance.py` for the
offline gate; its success marker is `GPT_CODEX_BLUEPRINT_REVIEW_PASS`.

## Step 15 Stage planning boundary

```text
verified Bootstrap outputs
  (.research/PROJECT_BRIEF.json, PROJECT_CONTEXT.md,
   discovery/DISCOVERY_REPORT.json, blueprint/*, bootstrap_state.json)
          │ local digest and invariant checks
          ▼
  src/stage_planning.py ──► one stage_contract.v1 + StageController.prepare_stage
                                      │
                                      ▼
                 .research/stages/<stage-id>/contract.json
                 .research/stage-state.json (status=PLANNED)
                 .research/stage-planning/STAGE_PLAN.json
```

Step 15 is an explicit, local planning operation. It consumes only verified
canonical Bootstrap outputs, derives one deterministic Stage contract, and
registers it through the existing `StageController`; it never calls the
bridge, executes a route, changes business/source files, or starts a Stage.
The contract records the project and route goals, repository-bounded paths,
required checks, review artifact roles, and an immutable digest baseline for
the Bootstrap evidence. `scripts/stage_cli.py plan-stage` and
`scripts/step15_acceptance.py` are the product and offline acceptance entry
points. Repeating the operation with unchanged evidence reuses the existing
plan; a changed or tampered input fails closed. The only next action emitted
by the planner is the user's explicit `start` command.
