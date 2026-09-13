# Repository cleanup dry run

This is a read-only inventory and cleanup proposal for Product repository baseline `9203147` at `D:\work\research_tools\research_supervisor_v2_product`.

The inventory snapshot cutoff is `2026-09-12T12:32:15.035182+00:00`. The physical snapshot contains file metadata and Git tracked/untracked/ignored status. Subsequent classification review read selected test and script source to distinguish reusable regression assets from date-fixed evidence producers. Credential values and external runtime contents were not read. `.git` and the `audit` output directory were excluded from physical traversal; `audit` was absent at the cutoff and was created afterward for these artifacts. No move, archive, quarantine, or delete operation was performed.

The complete per-file and per-directory records are [repository-inventory.tsv](audit/repository-inventory.tsv), [repository-inventory.json](audit/repository-inventory.json), and [directory-inventory.tsv](audit/directory-inventory.tsv). The machine-readable summary is [cleanup-dry-run-summary.json](audit/cleanup-dry-run-summary.json).

## Snapshot totals

| Measure | Files | Logical bytes |
|---|---:|---:|
| All inventoried files | 303 | 6,317,262 |
| Tracked | 218 | 3,827,285 |
| Untracked (excluding audit output) | 6 | 71,439 |
| Ignored | 79 | 2,418,538 |

There are 60 directories including the repository root and 59 directories below the root. Logical bytes are the sum of file sizes at the cutoff; they are not disk allocation figures.

## Exclusive category counts

| Category | Files | Logical bytes | Meaning in this dry run |
|---|---:|---:|---|
| ENGINE | 169 | 2,967,130 | Reusable engine, contract, fixture, skill, launcher, or regression asset |
| PROJECT | 3 | 28,491 | Project identity or authoritative project runtime state |
| HISTORY | 68 | 967,680 | Receipts, implementation evidence, and historical validation material |
| TEMP | 63 | 2,353,961 | Regenerable caches, locks, or staging candidates |
| DERIVED | 0 | 0 | No separate derived class identified in this snapshot |
| UNKNOWN | 0 | 0 | No unclassified file; uncertain source roles were retained conservatively as ENGINE/KEEP |

## Exclusive action counts

| Proposed action | Files | Logical bytes | Dry-run interpretation |
|---|---:|---:|---|
| KEEP | 192 | 3,069,157 | Retain at the current owning path |
| MOVE | 46 | 851,858 | Long-term evidence belongs in the owning self-host validation project |
| MERGE | 0 | 0 | No merge proposed |
| RENAME | 0 | 0 | No rename proposed |
| ARCHIVE | 2 | 42,286 | Date-fixed baseline/evidence producers should be retained in history |
| DELETE_CANDIDATE | 63 | 2,353,961 | Candidate only; requires an active-writer, receipt, and run verification before any later action |
| UNKNOWN | 0 | 0 | No unresolved action |

The category and action columns are exclusive for every row. `DELETE_CANDIDATE` is a review label, not authorization to delete.

## Top-level directory summary

The table omits root-level files; the complete TSV contains every inventoried row.

| Path | Files | Bytes | Action counts | Category counts |
|---|---:|---:|---|---|
| `.consultations` | 9 | 13,476 | KEEP 1; DELETE_CANDIDATE 8 | HISTORY 1; TEMP 8 |
| `.pytest_cache` | 4 | 7,817 | DELETE_CANDIDATE 4 | TEMP 4 |
| `.research` | 14 | 44,011 | KEEP 14 | PROJECT 2; HISTORY 12 |
| `.workflow-v2` | 2 | 18,654 | KEEP 1; DELETE_CANDIDATE 1 | PROJECT 1; TEMP 1 |
| `examples` | 1 | 526 | KEEP 1 | ENGINE 1 |
| `fixtures` | 5 | 5,379 | KEEP 5 | ENGINE 5 |
| `implementation_evidence` | 46 | 851,858 | MOVE 46 | HISTORY 46 |
| `schemas` | 43 | 106,863 | KEEP 43 | ENGINE 43 |
| `scripts` | 20 | 413,553 | KEEP 17; ARCHIVE 2; DELETE_CANDIDATE 1 | ENGINE 17; HISTORY 2; TEMP 1 |
| `skills` | 2 | 10,018 | KEEP 2 | ENGINE 2 |
| `src` | 81 | 3,771,004 | KEEP 44; DELETE_CANDIDATE 37 | ENGINE 44; TEMP 37 |
| `tests` | 63 | 979,818 | KEEP 51; DELETE_CANDIDATE 12 | ENGINE 51; TEMP 12 |

The cache rows under `src` and `tests` are ignored `__pycache__` files. They do not represent source deletions.

## Main path decisions

| Path | Current role | Problem or boundary | Owner | Target | Action |
|---|---|---|---|---|---|
| `src` | Reusable V1/V2 implementation and Product adapter | None identified | Product engine repository | `src` | KEEP |
| `schemas` | Reusable contracts and schemas | None identified | Product engine repository | `schemas` | KEEP |
| `scripts` | Reusable launchers/CLI mixed with one-off phase harnesses | Three baseline/real E2E scripts remain history; acceptance and regression helpers are reusable | Product engine plus self-host history | Split by per-file classification | KEEP / ARCHIVE |
| `tests` | Reusable regression suites plus regenerable interpreter cache | Source tests are reusable; cache rows are temporary | Product engine repository plus machine-local cache | Tests source stays; cache candidate only | KEEP |
| `fixtures` | Deterministic reusable fixtures | None identified | Product engine repository | `fixtures` | KEEP |
| `examples` | Reusable configuration/example assets | None identified | Product engine repository | `examples` | KEEP |
| `skills` | Existing repository skill assets | No exact `research-workflow` default was found; preserve the existing exact assets | Product engine repository | `skills` | KEEP |
| `implementation_evidence` | Long-term implementation and self-host evidence | Large evidence set is mixed into the engine checkout | Owning engine self-host validation project | `<owning-engine-development-project>/implementation_evidence` | MOVE |
| `.research` | Project identity plus current task history | Identity-bound files must remain project-owned | Product workspace | Same `.research` paths until an approved migration binds the spec digest | KEEP |
| `.workflow-v2` | V2 journal plus lock scratch | Journal is authoritative; lock needs an inactive-writer check | Product workspace runtime | Journal stays; lock is a quarantine candidate | KEEP / DELETE_CANDIDATE |
| `.consultations` | Sanitized receipts plus upload staging | Receipt provenance must remain; staging is regenerable | Project history plus machine-local staging | Receipts stay; staging is a quarantine candidate | KEEP / DELETE_CANDIDATE |
| `.pytest_cache` | Regenerable test cache | No source value | Machine-local test runtime | Regenerate as needed | DELETE_CANDIDATE |

`implementation_evidence` is history with long-term ownership. It is not garbage and is not a deletion candidate. Consultation receipts remain history; only staging packs are candidate duplicates, and this report performs no discard.

### Source-purpose review

The first pass overclassified files whose names contained `step`, `scenario`, or `harness`. The ten archived test files were read and retained as ENGINE/KEEP because they exercise reusable contracts, supervisor policies, context/artifact safety, discovery, blueprint, planning, Git, and bridge-integration behavior:

| Test file | Source evidence used for classification |
|---|---|
| `tests/test_disposable_openai_codex_e2e.py` | Imports the disposable executor fixture and asserts bounded source/test/instruction behavior |
| `tests/test_scenarios.py` | Exercises `src.contracts` and `src.supervisor` scenarios and schema/policy invariants |
| `tests/test_stage9_real_smoke_harness.py` | Offline bridge-root forwarding regression against the smoke helper |
| `tests/test_step12_context_hygiene.py` | Offline retention, context, schema, and secret-boundary regression |
| `tests/test_step13_discovery.py` | Offline `src.project_discovery` response, path, receipt, and verifier regression |
| `tests/test_step14_blueprint.py` | Offline blueprint precondition, grammar, path, and manifest regression |
| `tests/test_step15_planning.py` | Bootstrap-to-Stage planning boundary regression |
| `tests/test_step8_context_git.py` | Git baseline, stage context, artifact, and tamper-boundary regression |
| `tests/test_step9_harness.py` | Fresh/minimal context-pack guard regression |
| `tests/test_step9_integration.py` | Local orchestration, receipt, subprocess hygiene, and decision guards |

The tests directly import and therefore retain these reusable script assets as ENGINE/KEEP: `scripts/disposable_openai_codex_e2e.py`, `scripts/research_turn_real_e2e.py`, `scripts/stage9_real_smoke.py`, and `scripts/step15_acceptance.py`. The Step 10–14 scripts are also ENGINE/KEEP because their source is an offline, repeatable acceptance gate over Product contracts and local regression checks. The parameterized real validation harnesses `scripts/bootstrap_real_e2e.py`, `scripts/phase7_real_self_host.py`, and `scripts/phase7_scenarios.py` are retained as ENGINE/KEEP after source review; their generated evidence remains HISTORY. The only archived scripts are `scripts/fresh_v2_runtime.py` and `scripts/phase0_manifest.py`, whose date-fixed workspace/output and baseline path behavior make them historical evidence producers.

Within `.research`, the path-based classification keeps `PROJECT_BRIEF.json` and `workflow-state.json` as PROJECT-owned identity/runtime records and classifies the remaining twelve generated planning/request/bootstrap records as HISTORY. This distinction uses names, status, and metadata only; their contents were not read.

## Absent named paths at the cutoff

These paths were checked by existence metadata and were absent at the snapshot. The `audit` entry is intentionally special: it was absent when the snapshot was taken and exists now because this report was generated.

| Path | Kind | Interpretation |
|---|---|---|
| `.research/runtime-composition.json` | file | No project-local runtime config at cutoff; implicit defaults would remain V1 |
| `.research/stage-state.json` | file | No legacy V1 stage state at cutoff |
| `.research/bootstrap_state.json` | file | No legacy bootstrap state at cutoff |
| `.research/PROJECT_CONTEXT.md` | file | No named project context artifact at cutoff |
| `.research/discovery/DISCOVERY_REPORT.json` | file | No named discovery report at cutoff |
| `.research/blueprint/PROJECT_BLUEPRINT.md` | file | No named blueprint at cutoff |
| `.research/blueprint/BLUEPRINT_MANIFEST.json` | file | No named blueprint manifest at cutoff |
| `.research/stages` | directory | No named legacy stages directory at cutoff |
| `.agents/skills/research-workflow` | directory | No installed exact skill-source path at cutoff |
| `skills/research-workflow` | directory | No exact repository skill path at cutoff |
| `audit` | directory | Created after the cutoff for the excluded audit outputs |

## Post-cutoff output delta

`V2_PRODUCT_REPOSITORY_ARCHITECTURE.md` was created after the cutoff (observed at approximately `2026-09-12 20:33:11 +08:00`) and is therefore excluded from the 303-file totals. The already-present `V2_SPEC_KIT_ADOPTION_DECISION.md`, Product bootstrap readiness/failure artifacts, Product adapter, and bootstrap tests are included where they existed before the cutoff. Audit outputs under `audit/` are excluded by policy.

No repository cleanup action is authorized by this dry run. Any future move, archive, quarantine, or deletion must be a separate reviewed operation with the per-file TSV as its input.

## Pre-review delta and evidence provenance

The base totals above remain the exact original snapshot, not a claim about the current growing working tree. A later named-file delta is recorded in `.research/product-bootstrap/post-cutoff-inventory.json`: 13 additional Stage deliverable/validation files were observed at 2026-09-12T13:00:58.700536+00:00. Each is HISTORY/KEEP; none is a cleanup candidate. Audit output files, the delta itself, and later consultation/assessment/gate artifacts are outside these snapshots and remain retained Stage evidence. No deletion may use this older manifest without a fresh full inventory and reference check.

Named directory existence was also checked: `docs/`, `templates/`, and `.tmp/` are absent in this candidate at pre-review validation. Absence is not a proposal to delete or a missing file count; their target roles are described in the architecture. There is no nested candidate worktree directory in the inventoried tree.

Historical Phase-0 manifests preserve baseline `15fdb58cfbe91987e3a5112a9937e716bf265f59`. They are earlier HISTORY, not the current source authority. Current baseline remains `9203147ec2db6f990500c1995cffc61fbba2d595`; current proof is the actual Git diff and `.research/product-bootstrap/source-and-freeze-validation.json`. The old manifests are neither rewritten nor used to claim current readiness.
