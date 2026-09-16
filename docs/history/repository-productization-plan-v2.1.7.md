# Repository Productization Plan

Audit scope: the final GitHub productization pass after
`workflow-v2.1.6-stable`. This file records the inventory and the evidence
behind every planned move or removal. It is not runtime documentation and
must not become a second Workflow authority.

## Baseline

| Item | Value |
| --- | --- |
| Source stable | `workflow-v2.1.6-stable` |
| Source commit | `a5a1486ac9c7b153de5a3901feb74802ec9f9456` |
| Working branch | `workflow-v2.1.5-release-candidate` |
| Working tree | clean before this audit |
| Public remote | `https://github.com/ddl-beginning/research_workflow.git` |
| Remote main before release | `4dbd89b220fc94fd98295780666bea7c07a961b4` |
| Stable tag remote state | `workflow-v2.1.6-stable` was local-only at audit start; it must never be moved or overwritten |

## Classification inventory

The repository contained 323 tracked files before this pass. The following
categories define ownership and the release treatment.

| Classification | Tracked paths | Release treatment |
| --- | --- | --- |
| `PRODUCT_RUNTIME` | `src/`, `bridge/src/`, `bridge/scripts/`, `bridge/package.json`, `bridge/package-lock.json` | Keep |
| `PRODUCT_INSTALLATION` | `pyproject.toml`, `install.ps1`, `scripts/workflow.py`, `scripts/research_workflow_cli.py`, `scripts/product_doctor.py`, `src/portable_startup.py`, `skills/workflow-launcher/` | Keep and harden |
| `PRODUCT_USER_DOCUMENTATION` | `README.md`, `RELEASE_NOTES.md`, `docs/PROJECT_PLAN_INGESTION_CONTRACT.md` | Keep/rewrite |
| `PRODUCT_DEVELOPER_DOCUMENTATION` | `ARCHITECTURE.md`, `INSTALL.md`, `QUICKSTART.md`, `TROUBLESHOOTING.md`, `WORKFLOW_DEMO.md`, `docs/AUTONOMOUS_OBJECTIVE_COMPLETION_LOOP.md` | Move into `docs/` or `docs/history/`; rewrite user-facing copies |
| `PRODUCT_TEST` | `tests/`, `bridge/test/`, `fixtures/`, `bridge/test/fixture_project/`, `bridge/test/fixtures/` | Keep; fixtures are synthetic and reusable |
| `REUSABLE_FIXTURE` | `examples/`, `templates/`, `schemas/`, `examples/`, synthetic JSON fixtures | Keep |
| `HISTORICAL_DEVELOPMENT_EVIDENCE` | root candidate/audit/closeout reports; `.specify/`; `audit/`; `implementation_evidence/`; `specs/` | Remove from current product tree after reference audit; Git history remains |
| `SELF_HOST_PROJECT_ARTIFACT` | generated `.research/`, `.workflow-v2/`, `.consultations/`, and embedded self-host journals under evidence | Ignored or removed from tracked product tree; never copy into a business project |
| `TEMPORARY` | `.tmp/`, caches, generated `*.egg-info/` | Ignore/remove if tracked; never publish |
| `SECRET_OR_MACHINE_LOCAL` | `.auth/`, browser profiles, cookies, tokens, machine configs, runtime bridge state | Ignore and secret-scan; none may be published |
| `UNKNOWN` | Any path not covered by the table above | Keep until a separate evidence-backed decision exists |

## Planned document moves

| Current path | Target path | Reason | Audit status |
| --- | --- | --- | --- |
| `ARCHITECTURE.md` | `docs/history/architecture-v2.1.6.md` | durable historical architecture, not first-run setup | PASS; `git mv`, no product-path links remain |
| `INSTALL.md` | `docs/installation.md` | product installation reference | PASS; rewritten and audited |
| `QUICKSTART.md` | `docs/quickstart.md` | product quickstart reference | PASS; rewritten and audited |
| `TROUBLESHOOTING.md` | `docs/troubleshooting.md` | product troubleshooting reference | PASS; rewritten and audited |
| `WORKFLOW_DEMO.md` | `docs/development.md` | reusable developer example | PASS; `git mv` |
| `docs/AUTONOMOUS_OBJECTIVE_COMPLETION_LOOP.md` | `docs/outer-loop-contract.md` | runtime-loaded developer contract; keep out of README | PASS; runtime path updated |
| `RELEASE_VALIDATION.md` | `docs/history/release-validation-v2.1.6.md` | release evidence, not a user guide | PASS; `git mv` |
| `MODEL_ROUTING_AUDIT.md` | `docs/history/model-routing-audit-v2.1.6.md` | implementation audit; not a runtime instruction | PASS; audit script updated |
| `V2_PRODUCT_REPOSITORY_ARCHITECTURE.md` | `docs/history/v2-product-repository-architecture.md` | historical repository design record | PASS; broken audit link removed |
| `V2_SPEC_KIT_ADOPTION_DECISION.md` | `docs/history/v2-spec-kit-adoption-decision.md` | historical tooling decision | PASS; historical link updated |

## Planned removals

The following are historical process reports or generated evidence. Each must
be checked with runtime-reference, test-reference, documentation-link,
installer/package-reference, and Git-tracked-status searches immediately
before removal.

* `.specify/`
* `audit/`
* `implementation_evidence/`
* `specs/`
* `BOOTSTRAP_HANDOFF.md`
* `CANDIDATE_BASELINE.md`
* `CLEANUP_EXECUTION_REPORT.md`
* `FINAL_REPOSITORY_CLEANUP_CLOSEOUT.md`
* `OVERNIGHT_PRODUCT_V2_REPORT.md`
* `PHASE_0_BASELINE.md`
* `PRODUCT_RUNNER_BOOTSTRAP_READINESS.md`
* `PRODUCT_RUNNER_CANONICAL_FAILURE.json`
* `PROJECT_HANDOFF.md`
* `REAL_SELF_HOST_VALIDATION_REPORT.md`
* `REPOSITORY_CLEANUP_DRY_RUN.md`
* `V2_LIFECYCLE_IMPLEMENTATION_CLOSEOUT.md`
* `V2_PORTABILITY_AUDIT.md`

These removals are current-tree cleanup only. Git history and the immutable
`workflow-v2.1.6-stable` object remain preserved.

Reference audit result: no deleted root report is imported or opened by the
product runtime, installer, or public README/docs. The one runtime optional
source named `PROJECT_HANDOFF.md` was removed from the anchor allow-list before
that historical file was deleted. The `.specify/` and `specs/` names still
appear only as supported Business Project guidance paths or historical fixture
classification rules; the reusable Spec Kit fixture was moved to
`fixtures/guidance/` and its test/preflight references were updated. The
`implementation_evidence/` and `audit/` mentions remaining in scripts are
output/classification rules for explicitly invoked historical harnesses, not
checked-in product dependencies.

## Keep decisions

`src/`, `bridge/`, `schemas/`, `scripts/`, `skills/`, `templates/`,
`examples/`, `fixtures/`, `tests/`, `pyproject.toml`, `install.ps1`,
`README.md`, and `RELEASE_NOTES.md` are core product assets. No deletion is
allowed based only on root cleanliness. `UNKNOWN` paths remain kept by
default.

## Completed audit facts

* Root tracked files: 28 before cleanup, 6 after cleanup.
* Tracked files: 323 before cleanup, 286 after cleanup (moved fixture files
  remain tracked under `fixtures/`).
* `scripts/documentation_audit.py --root .`: PASS.
* Deleted paths were removed from the current tree only; Git history is
  preserved and no stable tag was moved or rebuilt.

## Validation gates

Before publish: `git status`, `git diff --check`, targeted and full tests,
repository secret scan, exact command validation, fresh GitHub clone, fresh
shell PATH check, new-project two-file smoke, existing-project smoke,
wrong-directory/typo/ambiguity diagnostics, and browser-close recovery smoke.

Required final markers:

* `PUBLIC_REPOSITORY_CLEANUP: PASS`
* `README_CANONICAL_FLOW: PASS`
* `NEW_MACHINE_INSTALLATION: PASS`
* `GITHUB_PUBLICATION: PASS`
