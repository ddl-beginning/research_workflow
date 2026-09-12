# Phase 6 Fresh V2 Runtime Evidence

The fresh runtime is candidate-only and has a new workspace identity:
`workspace-fresh-v2-20260912`. Its Journal was initialized at revision 0 with
zero Stages, and it has separate bounded requirement, design, and planning
acceptance fixtures plus fixed engine/validator manifest digests.

Evidence:

- `fresh_v2_runtime/runtime-manifest.json`
- `fresh_v2_runtime/journal.json`

The frozen `.research` tree was not copied and no old Parent, Attempt, budget,
or receipt was resumed. Initialization is idempotent and persists no lifecycle
event; the Runtime projection cannot create a Stage. The completed journal was
reloaded through `StageController.from_state`; the persisted projection digest
matched and the reloaded revision remained 9.
