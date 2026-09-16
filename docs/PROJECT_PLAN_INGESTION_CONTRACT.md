# Project Plan Ingestion Contract

This is a bounded convenience layer over the existing Workflow V2 runtime. It
does not create a second lifecycle, journal, planner authority, or project
state machine.

## User planning surface

The only files a project user must provide are:

```text
<PROJECT_ROOT>/plan/REQUIREMENTS.md
<PROJECT_ROOT>/plan/STAGE_PLAN.md
```

Both files are ordinary Markdown. `REQUIREMENTS.md` describes the final goal,
scope, non-goals, outputs, quality constraints, business constraints, and
acceptance. `STAGE_PLAN.md` describes each Stage, its stable ID, goal, inputs,
data or paths, tasks, outputs, acceptance, Human-gate requirement, and next
Stage. The parser supplies only technical structure; it does not rewrite user
meaning. If a Stage has no explicit ID, Workflow derives a stable slug from
its heading and keeps that binding in subsequent runs.

## Discovery and generated views

Every `workflow`, `workflow init`, and `workflow resume` checks the fixed
`plan/` path. If both sources exist, Workflow ingests them. If exactly one is
missing, it reports that exact relative path. If neither exists, the legacy
no-plan behavior is preserved.

Workflow maintains exactly two human-readable derived views:

```text
plan/WORKFLOW_PLAN.md
plan/CURRENT_STATE.md
```

`WORKFLOW_PLAN.md` is a normalized, executable view of the two user sources.
It has exactly two readable levels: `LEVEL 1 — PROJECT ROADMAP` with the
Project Goal, Scope, and a fixed Stage Roadmap table, followed by `LEVEL 2 —
STAGE CARDS`. Every card uses the same eleven sections: Goal, Why This Stage
Exists, Entry Conditions, Inputs (Data and Prior Accepted Evidence), Work To
Perform, Expected Outputs (Human-visible and Machine-readable), Machine
Evaluation (Primary and Secondary), Human-visible Evidence, Pass Gate, Replan
/ Stop Conditions, and On PASS. Tasks are rendered as bounded `T01`, `T02`, …
actions inside the card; ordinary coding, debugging, helper changes, and
single command retries are not promoted to Stages.

The normalized card records each Stage's stable ID, data name/path/role/GT
availability/reference type/data maturity, dependencies, expected evidence,
Human Gate, and next Stage. A read-only structural plan analysis checks stable
identity, dependency cycles, explicit data/output/gate/replan fields, and
`STAGE_SELF_CONTAINED_EXECUTION_READINESS`. Technical defaults are marked as
bounded normalization; they do not expand the product requirement or mutate
either user source.

`WORKFLOW_PLAN.md` never overwrites either source.

`CURRENT_STATE.md` is a readable snapshot containing Current Stage, Current
Status, Completed and Accepted Stages, Current Work and Progress, Current
Blocker, Next Legal Action, Remaining Stages, and the source digests. Its
header states:

```text
DERIVED HUMAN-READABLE SNAPSHOT

This file is not lifecycle authority.

Canonical runtime authority remains:
StageController + journal + contracts.
```

The journal and canonical projection win every conflict. A missing or stale
`CURRENT_STATE.md` is regenerated; it is never used to mutate the journal.

`CURRENT_STATE.md` is kept short and uses Project, Current Stage, Completed
Stages, Current Work, Remaining Stages, and Plan Status sections. It is a
derived snapshot only and always states `DERIVED HUMAN-READABLE SNAPSHOT` and
the canonical authority boundary. Stage closeout and Human approval refresh it
through the normal Workflow completion path before selecting the next legal
planned Stage.

## Binding and Stage transitions

The existing `StageController` remains the only lifecycle authority. Ingested
Stage descriptions are converted to the existing bounded `stage.v2` object and
registered through `REGISTER_STAGE`. No dependency registry or plan manifest
is created. The plan order is used only to select the next already-registered
Stage after the current Stage becomes terminal. A completed/accepted Stage in
the journal is never restarted. When a canonical Stage completion is observed,
Workflow refreshes both derived views and starts the next legal planned Stage
automatically when no Human Gate is pending. A Human approval continues through
the existing `APPROVE`/decision contract and then triggers the same refresh and
next-Stage selection.

Stage data paths are extracted directly from `STAGE_PLAN.md` and checked when
the Stage is entered. Existing paths are used. Missing Stage-owned paths are
work remaining and remain eligible for bounded automatic generation; only a
proven Human-only input or genuine external unavailability can block.

## Existing projects and plan changes

An existing project uses the same discovery path. Workflow reads the plan,
journal, Git and available evidence, then binds missing plan Stages without
replaying completed journal Stages. A project with existing accepted canonical
history is resumed from that history. If no journal exists, inspection is
bounded and only establishes a new canonical starting point; it does not
claim scientific completion from Markdown alone.

The two source digests are retained in the derived plan view. A source change
is detected on every run. Changes limited to future, not-started Stages update
the derived plan and continue. A change touching the current Stage, a
completed/accepted Stage, or the final objective is recorded as requiring
technical/plan review; historical journal records are not rewritten. The
derived views may record an interpretation, but never edit user sources.

## Resume and context recovery

On resume or context recovery, Workflow reloads, in order: this contract and
the outer-loop contract, `REQUIREMENTS.md`, `STAGE_PLAN.md`,
`WORKFLOW_PLAN.md`, the canonical journal/projection, `CURRENT_STATE.md` as a
derived cross-check, and the latest receipts. It then re-derives the current
Stage and next legal action from the controller. Missing derived views are
regenerated automatically.

## Per-project ChatGPT browser target

The optional Project target is configured in the same user-owned
`plan/REQUIREMENTS.md` file:

```text
## Workflow Binding
ChatGPT Project URL: https://chatgpt.com/g/g-p-example/project
```

The accepted shape is exactly an absolute `https` URL of the form
`https://chatgpt.com/g/g-p-.../project`. Credentials, query strings,
fragments, whitespace, the homepage, `/c/...` conversation routes, legacy
`/projects/...` routes, third-party origins, and path-normalization ambiguity
are rejected before a browser is opened. The target is parsed once and
projected into the existing canonical `.research/PROJECT_BRIEF.json` brief as
`chatgpt_project_url` plus a bounded binding record. There is no second
configuration file, project pool, rotation, quota/account switch, or profile
switch.

When the canonical brief contains a target, the runtime gives it precedence
over any machine-level default and passes the exact normalized URL to the
browser bridge. The bridge first visits `https://chatgpt.com/` only to hydrate
the authenticated session, then navigates to that exact target, verifies the
Project-scoped composer, creates a fresh conversation in that Project for each
NORMAL/TECHNICAL review, and requires the resulting receipt to prove the
binding. The homepage is never a prompt target. The invariant
`GPT_REVIEW_MUST_USE_BOUND_PROJECT_TARGET` is enforced at the runtime
boundary. Without a plan target, the prior default/homepage behavior remains
unchanged.

Receipts carry only bounded audit markers: `PROJECT` or `DEFAULT`, a SHA-256
target digest, the fixed origin, target verification, and whether a fresh
Project chat was created. Derived `WORKFLOW_PLAN.md` and `CURRENT_STATE.md`
show only `BOUND_PROJECT`/`CONFIGURED` (or the default state), never the full
URL. Browser cookies, tokens, passwords, and session files remain machine
local and are not committed or uploaded.

If the source digest changes the target, Workflow updates the canonical
binding for future consultations, records an append-only bounded change entry,
and leaves all prior consultation receipts byte-for-byte unchanged. A target
unavailable error may use the existing bounded bridge recovery path; it does
not become a Human technical relay. Only an irreducible authentication,
access, business, or acceptance gate can request Human involvement.

## GPT and Human boundaries

Local parsing, path resolution, existence checks, normalization, derived-view
generation, and Stage binding do not call GPT. The existing GPT Technical
Reviewer is called automatically only for genuine semantic ambiguity or a
plan change whose effect cannot be resolved locally. Human is requested only
for an irreducible business/semantic choice, explicit visual or semantic
acceptance, private input, or irreversible authorization. Human is not asked
to relay technical failures or restate the next Stage prompt.
