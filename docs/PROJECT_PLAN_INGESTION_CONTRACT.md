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
It includes Project Goal, Stage ID, Stage Goal, Inputs, Datasets / paths,
Tasks, Expected Outputs, Machine Acceptance, Human Acceptance, Human Gate
Required, Dependencies, and Next Stage. It never overwrites either source.

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

## GPT and Human boundaries

Local parsing, path resolution, existence checks, normalization, derived-view
generation, and Stage binding do not call GPT. The existing GPT Technical
Reviewer is called automatically only for genuine semantic ambiguity or a
plan change whose effect cannot be resolved locally. Human is requested only
for an irreducible business/semantic choice, explicit visual or semantic
acceptance, private input, or irreversible authorization. Human is not asked
to relay technical failures or restate the next Stage prompt.

