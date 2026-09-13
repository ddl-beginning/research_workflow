# Overnight Workflow V2 Product Report

Date: 2026-09-13 (Asia/Shanghai)

## Canonical state

- Product: `research_supervisor_v2_product`
- Frozen baseline: `9203147ec2db6f990500c1995cffc61fbba2d595`
- Workflow journal revision: `81`
- Resolver Stage: `project-artifact-resolver-v1` — `CLOSED`
- Portable validation Stage: `portable-product-validation-v1` — `CLOSED`
- Cleanup planning Stage: `legacy-repository-cleanup-v1` — `CLOSED`
- Final blocker: `HUMAN_DESTRUCTIVE_ACTION_GATE`

## Planning transport recovery

The original resolver planning receipt
`.consultations/CONSULT-20260912-151404-df1e1626/receipt.json` recorded
`ATTACHMENT_NOT_READY`, `request_count=0`, and no conversation identity. Offline
manifest checks matched all three attachments by size and SHA-256 and found no
forbidden path or secret. The bounded recovery reused the same semantic intent
and produced consultation `CONSULT-20260912-155646-18474f9a` with
`request_count=1`, validated conversation identity, the same planning digest,
and GPT `CONTINUE`. No semantic iteration was consumed by the failed transport.

## Resolver and portability

The resolver implementation added the single Product artifact placement seam,
NUL-segment protection, and Windows symlink/reparse escape protection. Its
final E2E evidence passed all checks, targeted tests were `29 passed, 1
skipped`, the real GPT Technical Review returned `STAGE_READY`, and its
integration receipt settled without destructive action.

The GPT-planned portable validation Stage proved relocated Engine placement,
Project A/B isolation, durable History, machine-local TEMP, legacy read-only
behavior, crash/resume idempotence, fresh inventory, engine cleanliness, and
frozen-kernel preservation. Targeted tests were `10 passed, 1 skipped`; all
checks were true and destructive actions were zero.

## Cleanup planning and final gate

The cleanup planner returned `CONTINUE` in consultation
`CONSULT-20260912-170848-810abad3` (`request_count=1`). A fresh read-only scan
then produced exact relative-path, hash-bound manifests:

- `450 KEEP`
- `46 MOVE`
- `0 ARCHIVE`
- `63 DELETE_CANDIDATE`
- `28 UNKNOWN` (preserved)

The real GPT Technical Review consultation
`CONSULT-20260912-171701-8224d023` returned `STAGE_READY` with
`request_count=1`. The settled integration receipt binds the exact cleanup
manifest. No MOVE, ARCHIVE, rename, or DELETE was executed; the final state is
`HUMAN_DESTRUCTIVE_ACTION_GATE`.

Evidence is under `.research/cleanup-planning/`, including
`cleanup-manifest.json`, the individual action manifests, the reference scan,
and the settled integration/closeout requests. The Product program is stopped
only at the explicitly required Human destructive-action boundary.

## Cleanup execution continuation

The execution Stage planning transport was recovered without changing semantic scope: the pre-prompt `ATTACHMENT_NOT_READY` remained `request_count=0`, and the same intent succeeded in `CONSULT-20260913-001653-a12051fe` with `request_count=1` and GPT `CONTINUE`.


The Human-authorized `legacy-repository-cleanup-execution-v1` Stage executed against the frozen manifest `a13597274d463b363f64e2e393df72ca22a77a37bf6eec39a56948ebf7648737` (freeze journal revision 81). MOVE completed 33 of 46 entries with exact copy/hash/rebind/unlink proof; 13 were deferred because the owning destination already contained different content. All 63 DELETE_CANDIDATE entries were copied with exact hashes into the machine-local quarantine with rollback metadata. Permanent deletion remained `0`.

The settled observation is `observation-6c1216ebdc048d496a70d5ddfd7c4113db7d2123befef250765baa4e28551148` with `request_count=1`. Resolver E2E, portable validation E2E, project isolation smoke, and the targeted suite (`75 passed, 1 skipped`) pass. Full regression is `519 passed, 1 skipped` with two known historical fixture failures caused by the absent fixed 2026-09-06 consultation receipt. The admissible assessment was recorded at journal revision 86. Real GPT Technical Review `CONSULT-20260913-004349-b405b7a9` returned `STAGE_READY` (`request_count=1`), integration settled, and canonical closeout completed at journal revision `90`.

The final blocker is `HUMAN_FINAL_DELETION_GATE`; permanent deletion is still unauthorized. Exact gate evidence is `.research/cleanup-execution/HUMAN_FINAL_DELETION_GATE.md`.

## Final quarantine deletion

Human decision `AUTHORIZE_FINAL_DELETION_OF_VERIFIED_QUARANTINE_ONLY` was bound to operation `operation-final-deletion-f619e04989b08c0215aab10d8e52a839fe83da62eba85e6a4570e7cc15990b62` and the frozen manifest `a13597274d463b363f64e2e393df72ca22a77a37bf6eec39a56948ebf7648737`. Final preflight verified all 63 classifications and quarantine identities; 24 entries were safe to delete and 39 were deferred because source paths reappeared or a canonical classifier reference was present. Exactly 24 quarantine files (504,542 bytes) were permanently deleted. Remaining quarantine count is 39 with exact hashes; unexpected files touched is 0. MOVE remains 33 completed / 13 deferred, UNKNOWN remains preserved, and no architecture or lifecycle file changed.

Post-delete validation passed for resolver, bootstrap, isolation, portability, guidance, V2 lifecycle, clean disposable bootstrap/resume, and engine pollution checks. Targeted tests remain `75 passed, 1 skipped`; full regression remains `519 passed, 1 skipped` with the two known historical missing-receipt fixture failures. `FINAL_DELETION_VALIDATION: PASS` is recorded in `FINAL_REPOSITORY_CLEANUP_CLOSEOUT.md`. No further cleanup is authorized.
