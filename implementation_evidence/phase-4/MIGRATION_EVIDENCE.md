# Phase 4 Migration Evidence

## Replacement-before-retirement record

| Superseded seam | V2 replacement | Evidence | V2 status |
| --- | --- | --- | --- |
| recovery/bootstrap lifecycle entrypoints | ordinary `StageController` commands and typed blockers | `tests/test_workflow_v2_controller.py`, `tests/test_workflow_v2_invariants.py` | retired from V2 route |
| review-only success shortcut | immutable Observation → Assessment → GPT Technical Review → READY | full canonical closeout test | retired from V2 route |
| recovery-specific result binder | one `ASSESS_RESULT` binder in the journal reducer | assessment identity and failed-observation tests | retired from V2 route |
| `bind_existing_execution_result` / automatic retry migration | `RECORD_OBSERVATION`, typed `RETRY`, or typed GPT `REPLAN` | retry and unknown-effect invariant tests | absent from V2 route |
| writable runtime checkpoint routing | `WorkflowRuntimeV2.resume_projection()` | Phase 3 projection evidence | retired from V2 route |

The isolated candidate was created from the verified HOST baseline and did not
copy the frozen V2 recovery/bootstrap implementation. The V1 compatibility
modules and their writers remain in the repository solely for the HOST
conformance regression suite and historical readers; they are not imported by
or allowed to write the V2 journal. This is an explicit compatibility boundary,
not a second V2 authority.

## Static V2 route audit

The V2 route is limited to `workflow_v2_contracts.py`,
`workflow_v2_controller.py`, `workflow_v2_runtime.py`, and
`workflow_v2_adapters.py`. The migration tests scan those modules for
superseded entrypoint names and forbidden writable lifecycle states. The only
V2 persistence call is `StageController._persist`, reached from the command
reducer; Runtime and adapters have no file-write calls.

## Phase 4 decision

PASS for V2 route retirement-by-exclusion and replacement coverage. Legacy V1
compatibility remains quarantined from the V2 entrypoint so the existing HOST
regression can be measured without claiming that its old state machine is the
V2 architecture.
