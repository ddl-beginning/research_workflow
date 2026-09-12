# Phase 1 Contract Matrix

## Scope

The candidate now contains the seven domain roots and three shared support
roots required by the accepted architecture under `schemas/workflow_v2/`.
`workflow_v2_contracts.py` is pure validation and identity derivation: it does
not persist state, dispatch a provider, or mutate a lifecycle record.

Typed failure and validator-correction receipt are nested contracts used by
the Provider Observation and shared Decision/Technical Review paths. They are
not additional lifecycle domain roots.

## Covered contract assertions

| Contract | Evidence |
| --- | --- |
| Five writable Stage states; no BLOCKED/SUSPENDED | `test_state_and_command_hard_limits`, `test_stage_rejects_writable_blocked_and_invalid_repair_depth` |
| Exactly 16 canonical public commands; OPEN_ITERATION/RETRY are internal mappings | `test_state_and_command_hard_limits`, `test_command_and_effect_envelopes_are_typed` |
| All machine budgets are explicit and non-negative | `stage.schema.json`, `validate_budget`, Stage fixture matrix |
| Objective fingerprint is independent of Stage id/wording | `test_objective_fingerprint_is_independent_of_stage_id_and_wording` |
| Provider FAILED observation is immutable; raw provider claim has no transition authority | `test_failed_provider_observation_is_immutable_and_raw_claim_has_no_authority`, `test_typed_failure_requires_controller_policy_and_effect_state` |
| Assessment identity is a stable complete tuple hash | `test_failed_observation_can_receive_new_assessment_without_provider_success_rewrite` |
| Revalidation creates a new assessment and never rewrites the FAILED observation | same test, with approved correction receipt |
| Missing evidence yields INSUFFICIENT without a new attempt | `test_missing_evidence_is_insufficient_and_not_a_provider_retry` |
| Validator correction cannot expand scope | `test_scope_expansion_cannot_be_validator_correction` |
| GPT decisions require persisted request/response/packet provenance | `test_decision_subject_and_typed_replan` |
| Stale decision subjects fail closed | `test_decision_subject_and_typed_replan` |
| Dependencies are bounded, acyclic and single-owner | `test_dependency_graph_is_bounded_acyclic_and_single_owner` |
| UNKNOWN/CONFLICT effects cannot be declared settled | `test_command_and_effect_envelopes_are_typed` |
| Required artifacts are distinct from changed-path scope, with protected paths rejected | `test_evidence_manifest_preserves_required_vs_changed_scope` |

## Verification

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_workflow_v2_contracts.py
13 passed in 0.29s
```

The focused suite is intentionally table/behavior driven rather than a JSON
snapshot mirror. The production route remains unchanged in this phase.

## Phase 1 exit decision

PASS. Phase 2 may add the durable reducer and storage protocol. The reducer
must consume these contracts, keep all lifecycle writes in one journal, and
preserve the distinction between observation bytes and assessment identity.
