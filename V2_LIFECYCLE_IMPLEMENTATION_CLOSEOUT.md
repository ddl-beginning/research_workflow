# Workflow V2 Lifecycle Implementation Closeout

Generated: 2026-09-12

## Decision

**PASS for the isolated candidate V2 lifecycle route.** The implementation
honors the accepted `ACCEPT_V2_LIFECYCLE_ARCHITECTURE` contract and keeps the
V2 authority in one durable `StageController` journal/reducer. No Spec Kit
program was started.

The acceptance is recorded as a user-task attestation in
`implementation_evidence/phase-0/human-architecture-acceptance.json`; it is
not fabricated as a runtime receipt.

## Contract and authority conformance

- Persistent domain concepts: 7 (`Stage`, `Semantic Iteration`, `Execution Attempt`, `Provider Observation`, `Stage Assessment`, `Decision`, `Dependency`).
- Writable Stage states: exactly 5 (`PLANNED`, `ACTIVE`, `READY`, `CLOSED`, `STOPPED`).
- Canonical public commands: exactly 16; `OPEN_ITERATION` remains an internal START operation and retry is typed `REQUEST_EXECUTION`.
- Shared guard families: 9.
- V2 lifecycle journal writers: 1; V2 assessment binders: 1; Human Decision resolver: 1; executable owner token: 1.
- V2 recovery/bootstrap lifecycles: 0; independently writable V2 projections: 0.
- The V2 route is isolated to `workflow_v2_controller.py`, `workflow_v2_contracts.py`, `workflow_v2_runtime.py`, and `workflow_v2_adapters.py`. Legacy V1 compatibility remains only for the baseline regression/history-reader surface and is not a V2 writer.

Safety controls implemented include immutable content-addressed observations,
versioned assessments, typed failure retryability, exact typed GPT REPLAN
choices, validator-correction Human binding, proof-bound external receipts,
subject-bound command idempotency, reload-before-CAS with a cross-process
journal lock, projection-digest reload checks, and bounded dependency
authorization/ownership/closeout/descendant attempts.

## Verification evidence

- V2 focused regression: **50 passed**.
- Full candidate regression: **477 passed, 2 failed**. Both failures are the pre-existing historical `workflow_self_test.py` fixture failures caused by the intentionally absent `.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`; no new V2 failure occurred. Raw output: `implementation_evidence/phase-5/full-isolated-pytest-final.stdout.txt`.
- Fresh V2 candidate-only runtime: new workspace `workspace-fresh-v2-20260912`, journal revision 0 before registration and revision 9 after the nine-command bounded lifecycle, final Stage `CLOSED`. The journal was reloaded and matched its authoritative in-memory state. Evidence: `implementation_evidence/phase-6/fresh_v2_runtime/journal.json` and `implementation_evidence/phase-7/self-host-validation.json`.
- The self-host run uses deterministic local provider/GPT fixtures. `external_provider_or_gpt_called` is explicitly `false`; no external credentialed or paid action was invented. The recorded routing contract is `STANDARD / gpt-5.6-luna / max`.

## Preservation boundary

The stable HOST `D:\work\research_tools\research_supervisor_poc` was not
modified. Its full normalized preservation digest remained
`1aaa2c1ad39e2bca3b69e956e8bed7a0c917e69dffcc963e3581ab94295b4c5b` across the
final recheck. The frozen V2 tree
`D:\work\research_tools\research_supervisor_v2_clean` was not modified: its
named `.research` hashes, Git HEAD `15fdb58cfbe91987e3a5112a9937e716bf265f59`,
and dirty count 67 remained unchanged. Evidence:
`implementation_evidence/phase-5/preservation-after.json`.

The only implementation changes are on candidate branch
`workflow-v2-lifecycle-candidate` at
`D:\work\research_tools\research_supervisor_v2_lifecycle_candidate`.

## Remaining limitation

The formal V2 route is verified, but the final self-host evidence is a local
bounded harness rather than a real external provider/GPT run. A future
credentialed external exercise requires a separate Human-authorized decision
and must preserve the same journal, receipt, effect-settlement, and scope
guards. This limitation does not authorize resuming the frozen Parent or
changing the accepted architecture.
