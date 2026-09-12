# Phase 2 Transition Evidence

## Canonical authority

`src/workflow_v2_controller.py:StageController.dispatch` is the only V2
transition entry. It validates the command envelope, acquires the journal
process lock, reloads the latest committed journal, checks the expected
revision, applies exactly one reducer handler, appends one content-addressed
event with a projection digest, and atomically replaces the journal. The
provider, GPT, Human and integration layers are represented as validated
payloads and receipts; none can write the journal directly.

The V2 journal is one JSON document with a monotonically increasing revision.
`_persist` writes and fsyncs a temporary sibling before `os.replace`, so a
persist failure leaves the in-memory and on-disk committed journal unchanged.
Reload validates schema, revision/event cardinality, every event identity, all
persisted domain records, command subject indexes, and the latest projection
digest. A command id is bound to its type, payload, and subject.

## Covered transitions

| Contract | Evidence |
| --- | --- |
| REGISTER_STAGE / START | controller scenario setup and `test_persisted_journal_reloads_with_zero_replay_and_closed_zero_dispatch` |
| REQUEST_EXECUTION | `test_full_canonical_execution_review_integration_closeout`, budget/owner checks |
| RECORD_OBSERVATION | same test; exact attempt/iteration linking |
| ASSESS_RESULT | same test and failed-result revalidation scenario |
| APPLY_GPT_DECISION | STAGE_READY, ENGINEERING_FIX and NEXT_ITERATION scenarios |
| ADVANCE_ITERATION | GPT NEXT_ITERATION path; no increment for CONTINUE/FAILED |
| REQUEST_DECISION / APPLY_DECISION | `tests/test_workflow_v2_invariants.py` Human subject/provenance tests; reducer stores one decision collection |
| ADD_DEPENDENCY / SATISFY_DEPENDENCY | child normal closeout and owner-return scenario |
| APPLY_RECEIPT | integration effect settlement and unknown-effect blocker paths |
| COMMIT_INTEGRATION / CLOSEOUT | complete closeout path; CLOSED cannot dispatch again |
| STOP | unknown external effect blocks stop; terminal path releases owner |
| crash before commit | `test_failed_commit_does_not_publish_partial_journal_and_tampered_events_are_rejected` |
| stale command / idempotency | `test_command_idempotency_and_stale_revision_are_fail_closed` |

## Invariants proven

- Provider `FAILED` bytes remain unchanged; revalidation appends a new
  assessment identity and never creates provider success.
- A failed observation is `REJECTED` unless an approved validator correction
  revalidation creates an admissible assessment; it cannot directly become
  `READY`.
- Assessment binding is only updated for `ADMISSIBLE`; the live candidate is
  the latest committed attempt.
- `ENGINEERING_FIX` creates a new attempt in the same iteration only after a
  typed GPT decision. `NEXT_ITERATION` creates exactly one new iteration with
  an explicit technical change identity.
- `UNKNOWN` and `CONFLICT` effect states block assessment/settlement and do
  not permit STOP or blind replay.
- A dependency child is a new PLANNED Stage, has one owner token, closes via
  the ordinary execution/review path, and returns the owner without mutating
  the Parent state or opening its next iteration.
- A failed reducer persist does not publish a partial journal; tampered event
  identities and tampered authoritative projections are rejected on reload.
- External terminal effects require settlement proof and receipt-byte binding;
  cross-process stale writers fail CAS after the latest journal is reloaded.
- Dependency insertion requires an accepted Human `DEPENDENCY` Decision,
  same-workspace child identity, one open child at a time, and bounded
  descendant attempts. Satisfaction binds the parent command and exact child
  closeout identity.

## Verification

```text
PowerShell:
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q tests/test_workflow_v2_contracts.py tests/test_workflow_v2_controller.py tests/test_workflow_v2_runtime.py tests/test_workflow_v2_invariants.py tests/test_workflow_v2_migration.py
50 passed (timing is environment-dependent)
```

## Phase 2 exit decision

PASS for the isolated canonical V2 controller path. Phase 3 exposes
`WorkflowRuntimeV2` through the runtime module and adapters while keeping all
route fields derived from this reducer. Legacy V1 writer seams remain only for
the baseline compatibility suite; Phase 4 records why they are not part of
the V2 route and proves the V2 path has no recovery/bootstrap writer.
