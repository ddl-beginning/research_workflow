# Workflow V2 Real Self-Host Validation Report

Generated: 2026-09-12 19:42 Asia/Shanghai
Candidate: `D:/work/research_tools/research_supervisor_v2_lifecycle_candidate`
Immutable implementation baseline: `2d5e416c54256511ee6791c658ca1db7430e5d7b`

## Final decision

**PASS.** The accepted Workflow V2 lifecycle implementation completed a real
external self-host happy path and all requested bounded scenarios A-E. No
architecture deviation or production correction was required after the
baseline. The only new code in this phase is validation tooling and evidence;
the V2 authority remains the single `StageController` journal/reducer.

## Real external happy path

Fresh disposable workspace:

`D:/work/research_tools/v2_real_selfhost_validation-20260912-191659`

The run used the existing authenticated formal browser bridge for fresh GPT
planning and technical review, and the real saved-ChatGPT Codex executor for
Provider execution. No historical `.research` state, project package, facade,
or V1 runtime was imported or used.

| Check | Evidence |
| --- | --- |
| Real planning GPT | `CONTINUE`; consultation `CONSULT-20260912-111701-36c59c8e`; conversation `6aa534d4-dc9c-83ee-a19d-171dc43e450c`; request count `1`; packet `b395579089f02e84e8ca849fdf80d2870129d096530614843f02361318a45e22` |
| Real Provider | `openai-codex`; executor request `request-command-real-request-20260912`; terminal `SUCCEEDED`; actual model `gpt-5.6-luna`; profile `STANDARD`; reasoning `max`; auth `chatgpt` |
| Provider scope | Exactly `src/add.py`; exact command `python -m pytest -q tests/test_add.py` passed |
| Immutable V2 observation | `observation-0990498eafca5577477cc8664c39e47481c21d33e70460c52939d1211f3413a6`; effect `SETTLED` |
| V2 assessment | `assessment-7b0c9c7e7d14d27781c43e44339ccb0751bff106ccfafd0848b1ade4eefc3c0e`; verdict `ADMISSIBLE` |
| Real technical GPT | `STAGE_READY`; consultation `CONSULT-20260912-111914-fafc8ba1`; conversation `6aa53553-608c-83e9-bf8e-545a8f0f3bc6`; request count `1`; packet `1100b68fba382220481b792c60e006c74fbffcd9ccb32dd15f6520311ee82e82` |
| Integration | Operation `operation-command-real-commit-integration-20260912`; local commit `0cf25de3e5599420c4f19168e0c5c0dd972cc6d2`; receipt effect `SETTLED` |
| Closeout/reload | Final journal revision `9`; `CLOSED`; executable owner `null`; reload projection identical; duplicate closeout dispatch count `0` |

Provider metadata-only artifacts are retained at
`implementation_evidence/phase-7/real-provider-artifacts-v2_real_selfhost_validation-20260912-191659/`:
`request.json`, `events.jsonl`, `workspace.diff`, and `run.json`. Raw prompt and
raw JSONL storage are both explicitly false. The bounded run evidence is
`../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-7/REAL_SELF_HOST_VALIDATION.json`.

## Scenario validation

The A-E run is recorded in
`../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-7/SCENARIOS_A_E.json`; its fresh journals are
under `implementation_evidence/phase-7/scenarios/run-20260912-203100/`.

| Scenario | Result and proof |
| --- | --- |
| A | **PASS** — first retryable engineering failure assessed `REJECTED`; second attempt is distinct, stays in the same iteration, preserves the first immutable observation, assesses `ADMISSIBLE`, and ends `STOPPED`. |
| B | **PASS** — one real fresh GPT review returned `WORKFLOW_DECISION: REPLAN` with typed `NEXT_ITERATION`; controller created exactly one new semantic iteration without inferring provider success. Consultation `CONSULT-20260912-113204-c4b00104`, request count `1`. |
| C | **PASS** — accepted Human `VALIDATOR_CORRECTION` enables revalidation of the same failed observation; old assessment remains, new assessment is `ADMISSIBLE`, provider attempt count remains `1`, and one fresh real GPT review returned `STAGE_READY`. Consultation `CONSULT-20260912-113253-ae7130d0`, request count `1`. |
| D | **PASS** — child dependency runs through normal lifecycle to `CLOSED`; dependency becomes `SATISFIED`; parent remains `PLANNED`, its Human decision remains pending, and executable ownership returns to the parent. |
| E | **PASS** — crash/reload preserves the same operation and attempt identities; unavailable external effect becomes `UNKNOWN` with an `UNKNOWN_EXTERNAL_EFFECT` blocker; assessment and stop are rejected, so no blind replay occurs. |

## Architecture gate

The final V2 route remains aligned with the accepted architecture:

- 7 domain schema roots and 3 shared support roots.
- Exactly 5 writable Stage states and 16 canonical public commands.
- 9 shared guard families.
- One durable V2 journal writer, one assessment binder, one Human Decision
  resolver, one executable owner token, and zero independent projection
  writers.
- Observations are immutable/content-addressed; assessments are versioned and
  bound to observations; typed failure, typed replan, receipt settlement,
  dependency, CAS, idempotency, and reload guards are enforced by the V2
  controller.

The baseline manifests are retained in
`../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-7/REAL_PHASE7_BASELINE.json`:

| Scope | Files | Normalized digest |
| --- | ---: | --- |
| Source | 43 | `f475f7a20e34ccad5da4e609e9923e03462f78adcb576c7a2360ed8c8867e9af` |
| Schema | 43 | `130d29aefe70bff57987d46d3d2042a4581cc036b8fa2cd950113466c61a94bd` |
| Tests | 49 | `216a07d15e42f95546b45eac80e7945273fc0a7d3b39b6cd3a8f46b1b7b32636` |
| Scripts | 17 | `6279fc7c8d93948afc43049feff7cd0887629343aa39acbbb4715b61aad55789` |

No production source/schema/test correction was made after that immutable
baseline. Harness-only corrections were limited to creating declared empty
context roots, using the existing homepage bridge fallback, binding the exact
provider capability set, disabling ambient pytest plugin autoload in the
bounded fixture, recording explicit `SETTLED` observation binding for GPT
review, and keeping the dependency parent unstarted so its pending Human gate
remained visible.

## Regression and preservation

- Focused V2 regression: **50 passed**; output in
  `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-7/final-v2-focused-pytest.stdout.txt`.
- Full candidate regression: **477 passed, 2 failed**; both failures are the
  pre-existing historical `workflow_self_test.py` cases requiring the absent
  `.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`. Output in
  `../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-7/final-full-pytest.stdout.txt`.
- HOST `D:/work/research_tools/research_supervisor_poc`: 158-file normalized
  digest remains `1aaa2c1ad39e2bca3b69e956e8bed7a0c917e69dffcc963e3581ab94295b4c5b`.
- Frozen V2 `D:/work/research_tools/research_supervisor_v2_clean`: named
  runtime hashes unchanged; HEAD remains
  `15fdb58cfbe91987e3a5112a9937e716bf265f59`; dirty count remains `67`.

The final preservation evidence is
`../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-7/preservation-final.json`. No HOST or frozen
V2 write was performed.

## Limitation

The happy path and B/C use real external GPT/Provider paths, while A/D/E use
fresh bounded controller fixtures by design. The browser bridge validates the
fresh conversation and one-request receipt but does not expose a separately
assertable model-selection field; therefore this report does not claim a GPT
model beyond the bridge’s validated real conversation identity. The bounded
fixture scope is not an architecture deviation.
