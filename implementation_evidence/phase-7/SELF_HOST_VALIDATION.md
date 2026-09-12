# Phase 7 Self-host Validation Evidence

The candidate executed the complete V2 lifecycle through the durable
controller Journal: `REGISTER_STAGE`, `START`, `REQUEST_EXECUTION`,
`RECORD_OBSERVATION`, `ASSESS_RESULT`, `APPLY_GPT_DECISION`,
`COMMIT_INTEGRATION`, `APPLY_RECEIPT`, and `CLOSEOUT`. The final Stage is
`CLOSED` at revision 9 with content-addressed event identities and a verified
projection digest. The same journal reloaded at revision 9 with identical
authoritative state.

This run is a local bounded self-host harness. The provider and GPT review
inputs are deterministic local fixtures, so `external_provider_or_gpt_called`
is explicitly `false`; no credential, paid action, or external transport was
invented. The recorded routing contract is the required STANDARD
`gpt-5.6-luna / max` route, and the evidence keeps provider observation
separate from the GPT technical Decision and Stage closeout.

The machine-readable evidence is
`implementation_evidence/phase-7/self-host-validation.json`; the fresh
Journal is under `implementation_evidence/phase-6/fresh_v2_runtime/journal.json`.
