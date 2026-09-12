# Workflow V2 Lifecycle Implementation Closeout

Generated: 2026-09-12

## Decision

**PASS.** The candidate Workflow V2 lifecycle route completed the real
external self-host happy path and bounded scenarios A-E. It honors the
accepted `ACCEPT_V2_LIFECYCLE_ARCHITECTURE` contract, keeps authority in one
durable `StageController` journal/reducer, and required no architecture
deviation or production correction after baseline `2d5e416`.

No Spec Kit program was started. The Human architecture acceptance remains the
user-task attestation in
`implementation_evidence/phase-0/human-architecture-acceptance.json`, not a
fabricated runtime receipt.

## Verification

- Real Provider: `openai-codex`, `STANDARD`, actual model `gpt-5.6-luna`,
  reasoning `max`, auth `chatgpt`; exact bounded test passed and only
  `src/add.py` changed.
- Real fresh GPT: planning `CONTINUE`, technical review `STAGE_READY`, each
  one request with validated fresh conversation identity.
- V2 journal: revision 9, settled integration, `CLOSED` after reload,
  projection identical, executable owner `null`, duplicate closeout effect 0.
- Scenario A: same-iteration retry preserves the failed observation.
- Scenario B: real GPT typed `NEXT_ITERATION` advances exactly once.
- Scenario C: accepted Human validator correction revalidates without provider
  rerun, then receives fresh real GPT review.
- Scenario D: child closes normally and returns ownership while the parent
  Human decision stays pending.
- Scenario E: crash/reload with unknown external effect creates a blocker and
  rejects assessment/stop; no blind replay.
- Focused V2 regression: **50 passed**.
- Full candidate regression: **477 passed, 2 failed**, both pre-existing
  historical missing-consultation-receipt fixtures; no new V2 failure.

Full evidence is in
`REAL_SELF_HOST_VALIDATION_REPORT.md`,
`implementation_evidence/phase-7/REAL_SELF_HOST_VALIDATION.json`, and
`implementation_evidence/phase-7/SCENARIOS_A_E.json`.

## Architecture conformance

The final gate remains 7 domain roots, 3 shared support roots, 5 writable
Stage states, 16 public commands, 9 guard families, one V2 journal writer,
one assessment binder, one Human Decision resolver, one executable owner, and
zero independent projection writers. Observations, assessments, typed
failure/replan, effect settlement, dependencies, CAS, idempotency, and reload
guards remain controller-enforced.

## Preservation boundary

HOST `D:\work\research_tools\research_supervisor_poc` remains unchanged:
158-file normalized digest
`1aaa2c1ad39e2bca3b69e956e8bed7a0c917e69dffcc963e3581ab94295b4c5b`. Frozen V2
`D:\work\research_tools\research_supervisor_v2_clean` remains unchanged:
named runtime hashes unchanged, HEAD
`15fdb58cfbe91987e3a5112a9937e716bf265f59`, dirty count 67. Final evidence:
`implementation_evidence/phase-7/preservation-final.json`.

The only Phase 7 additions are candidate validation tooling and bounded
evidence. A/D/E intentionally use local fixtures; the real external route and
real GPT bridge path are evidenced for the happy path and B/C. The bridge
does not expose a separately assertable GPT model-selection field, so this
closeout claims validated fresh GPT conversations, not an unobserved model
name.
