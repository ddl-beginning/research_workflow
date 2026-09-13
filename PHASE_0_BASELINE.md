# Phase 0 Baseline

Generated: 2026-09-12

## Candidate

- Path: `D:\work\research_tools\research_supervisor_v2_lifecycle_candidate`
- Branch: `workflow-v2-lifecycle-candidate`
- Baseline ref: `15fdb58cfbe91987e3a5112a9937e716bf265f59`
- Source baseline: normalized content at the baseline ref matches HOST manifest; later implementation files are controlled post-baseline changes.

## Preservation evidence

- HOST source/schema/test/script files: **131**, normalized manifest digest `2796596c77e1ac7b49dde36fa178841cb60461199743156a4d4d933853d85971`. HOST is not a Git checkout; preservation is verified by this digest and later re-checks.
- Candidate baseline files: **131**, normalized digest `2796596c77e1ac7b49dde36fa178841cb60461199743156a4d4d933853d85971`; equality with HOST: `True`.
- Candidate post-baseline source changes are enumerated in `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/candidate-post-baseline-delta.json`; they are controlled implementation diff, not baseline drift.
- Frozen V2 current source/schema/test/script files: **173**, digest `a543a40f7bc313f64dd63349deda5573300a5f778bdc314948b4348b56875364`; current Git dirty entries: **67**.
- Frozen V2 named runtime preservation hashes are recorded in `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/frozen-v2-preservation.json`; the full frozen history was intentionally not traversed.
- HOST full-tree preservation digest is recorded in `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/host-preservation-manifest.json`; HOST is not a Git checkout.
- `human-architecture-acceptance.json` records the explicit user-task decision with decision/subject/source identity and accepted scope; it is an attestation, not a runtime receipt.

## Baseline regression

- PowerShell: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q`: **427 passed, 2 failed**; raw stdout is saved in `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/baseline-pytest.stdout.txt`.
- Both failures are historical fixture failures: the clean candidate intentionally lacks `.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`; no synthetic receipt was copied.
- A first run without the ignored `.tmp` directory also failed before test execution with `FileNotFoundError`; the candidate-only empty `.tmp` directory was then created and the suite rerun.

## Evidence files

- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/host-source-manifest.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/host-preservation-manifest.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/v2-source-manifest.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/candidate-baseline-manifest.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/frozen-v2-preservation.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/human-architecture-acceptance.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/known-v2-safety-behavior-inventory.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/current-lifecycle-command-inventory.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/guard-inventory.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/schema-root-inventory.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/authority-writer-inventory.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/transition-test-traceability.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/before-after-inventory.json`
- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/baseline-pytest.stdout.txt`

- `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/candidate-post-baseline-delta.json`
## Phase 0 decision

PASS for isolated baseline creation and preservation checks. The explicit user-task acceptance is recorded as an attestation; no runtime receipt was fabricated. Candidate implementation changes after the HEAD baseline are tracked as the controlled Phase 1 diff. No provider/workflow dispatch was performed.
