# Workflow V2 Product Release Validation

Status: **PASS**  
Stage: `workflow-v2-product-release-validation-v1`  
Human decision: `ACCEPT_STAGE`  
Candidate: `workflow-v2-product-rc7`  
Candidate commit: `9f4a8b7c0cf375bd0f4741c0cf06b0b7b8511050`

## Clean-room result

The candidate was cloned into an external disposable validation root and
installed with an isolated editable Python environment. The clean checkout
doctor and documentation audit both passed. The Product engine remained Git
clean after the workflow run. No V1 host, frozen V2 checkout, quarantine item,
deferred MOVE item, UNKNOWN item, or cleanup-v2/v3 operation was touched.

External evidence: [RELEASE_VALIDATION.json](D:/work/workflow-v2-release-validation-20260913-r7/RELEASE_VALIDATION.json)

## Real execution path

| Gate | Evidence |
| --- | --- |
| Machine contract | Explicit V2 config; ChatGPT auth; Browser Bridge; Codex CLI; `gpt-5.6-luna` available |
| Intake | New project identity; explicit Human approval; no implicit Stage from `init` |
| GPT planning | Fresh consultation, one request, validated conversation, `WORKFLOW_DECISION: CONTINUE` |
| Provider | `openai-codex`; `STANDARD / gpt-5.6-luna / max / chatgpt`; `SUCCEEDED` |
| Provider scope | Changed path exactly `src/add.py`; required test exactly `python -m pytest -q tests/test_add.py` and PASS |
| V2 evidence | Immutable Observation `SETTLED`; Assessment `ADMISSIBLE` |
| GPT Technical Review | Fresh consultation, one request, validated conversation, `WORKFLOW_DECISION: STAGE_READY` |
| Integration | Explicit `COMMIT_INTEGRATION`; settled receipt; post-integration verification PASS |
| Resume | Fresh MCP process resumed the same Stage at integration intent; final journal revision preserved |
| Idempotency | Duplicate CLOSEOUT with identical command/payload produced no second effect |
| Closeout | `CLOSED`; Stage owner is null; project `specs/<stage-id>/CLOSEOUT.md` committed |
| Relocation | Relocated engine doctor and workflow resume passed; relocated engine remained Git clean |
| Transport | 19 process starts and 19 exits; one fresh MCP process per call |

## Regression

- Excluding the two historical `tests/test_workflow_self_test.py` fixture tests:
  `521 passed, 1 skipped`.
- Full suite: `521 passed, 1 skipped, 2 failed`.
- The two failures are the pre-existing fixed-receipt dependency on
  `CONSULT-20260906-070055-bef2fdb6`; they are documented in the prior
  cleanup/closeout reports. No synthetic receipt was created.

## Release boundary

This release validates the Product installation and lifecycle path. Codex CLI,
Browser Bridge, and headed ChatGPT authentication remain machine prerequisites;
the Product does not copy credentials or install those external services.
Historical retained cleanup state remains outside this stage and is not a
release blocker.
