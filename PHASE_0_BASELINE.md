# Phase 0 Baseline

Generated: 2026-09-12

## Candidate

- Path: `D:\work\research_tools\research_supervisor_v2_lifecycle_candidate`
- Branch: `workflow-v2-lifecycle-candidate`
- Base commit: `15fdb58cfbe91987e3a5112a9937e716bf265f59`
- Source baseline: clean HEAD normalized content matches HOST manifest.

## Preservation evidence

- HOST source/schema/test/script files: **131**, normalized manifest digest `2796596c77e1ac7b49dde36fa178841cb60461199743156a4d4d933853d85971`. HOST is not a Git checkout; preservation is verified by this digest and later re-checks.
- Candidate baseline files: **131**, normalized digest `2796596c77e1ac7b49dde36fa178841cb60461199743156a4d4d933853d85971`; equality with HOST: `True`.
- Frozen V2 current source/schema/test/script files: **173**, digest `a543a40f7bc313f64dd63349deda5573300a5f778bdc314948b4348b56875364`; current Git dirty entries: **67**.
- Frozen V2 key state hashes are recorded in the implementation closeout evidence; no frozen runtime files were copied or opened for write.

## Baseline regression

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`: **427 passed, 2 failed**.
- Both failures are historical fixture failures: the clean candidate intentionally lacks `.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`; no synthetic receipt was copied.
- A first run without the ignored `.tmp` directory also failed before test execution with `FileNotFoundError`; the candidate-only empty `.tmp` directory was then created and the suite rerun.

## Evidence files

- `implementation_evidence/phase-0/host-source-manifest.json`
- `implementation_evidence/phase-0/v2-source-manifest.json`
- `implementation_evidence/phase-0/candidate-baseline-manifest.json`
- `implementation_evidence/phase-0/known-v2-safety-behavior-inventory.json`
- `implementation_evidence/phase-0/current-lifecycle-command-inventory.json`
- `implementation_evidence/phase-0/guard-inventory.json`
- `implementation_evidence/phase-0/schema-root-inventory.json`
- `implementation_evidence/phase-0/authority-writer-inventory.json`

## Phase 0 decision

PASS for isolated baseline creation and preservation checks. Entry condition for Phase 1 is satisfied by explicit Human acceptance in the task request. No provider/workflow dispatch was performed.
