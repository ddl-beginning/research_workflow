# Phase 3 Projection and Adapter Evidence

`WorkflowRuntimeV2` is a read/submit facade over
`workflow_v2_controller.StageController`. It has no journal, status enum,
checkpoint writer, provider dispatcher, or decision resolver of its own.

The route fields `phase`, `next_action`, `next_actor`, `current_stage`,
`pending_gate`, and `attempt_count` are computed from `resume_projection()` on
the authoritative V2 journal. `read_legacy_checkpoint()` returns a detached
compatibility view only; mutating that returned object cannot alter routing.

`WorkflowV2ReceiptAdapter` forwards an observed receipt through
`APPLY_RECEIPT`. It does not settle an effect locally and cannot close a
Stage. The V2 class is also exported from `src.workflow_runtime` as an
explicit V2 entrypoint; the legacy `WorkflowRuntime` remains available only
for the pre-V2 compatibility suite and is not imported by the V2 facade.

Verification:

```text
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q tests/test_workflow_v2_runtime.py tests/test_workflow_v2_migration.py
```

The projection tests prove route derivation, detached legacy compatibility,
read-only resume, thin command submission, and fresh empty-journal
initialization. The migration tests prove that forbidden recovery/bootstrap
entrypoint names are absent from all V2 route modules.
