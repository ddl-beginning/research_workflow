# Autonomous Objective Completion Loop

Status: implementation and ownership-audit closure complete; bounded project-plan ingestion added; local release evidence complete; stable promotion held by the external clean-room review gate.

This document is a durable architecture and maintenance reference. It is not a
runtime authority. Runtime authority remains the canonical `StageController`,
the immutable journal, the contracts and schemas, the project brief, the
execution profile, and the existing canonical transition commands.

The design is intentionally bounded. It adds supervisory orchestration around
the existing lifecycle; it does not create another lifecycle, another journal,
or an unbounded retry mechanism.

## 1. Problem Statement

Workflow already has provider execution, GPT review, technical escalation,
objective binding, assessment supersession, stale-blocker recovery, automatic
GPT `CONTINUE` and `REPLAN` application, bounded retry, and iteration lifecycle.

The remaining architectural failure mode is an outer-supervision gap:

```text
intermediate technical failure
    -> FAIL report
    -> Codex turn ends
    -> Human asks GPT what to do
    -> Human tells Codex the next technical action
```

The technical message relay makes Human the outer supervisor. The target
architecture is for Workflow itself to remain the outer supervisor until the
objective is complete, a valid Human Gate is proven, or a genuine blocker with
no legal automated next action is proven.

## 2. Non-Negotiable Invariant

The canonical invariant is:

`UNFINISHED_OBJECTIVE_WITHOUT_HUMAN_BLOCKER_MUST_NOT_TERMINATE`

Its semantics are:

```text
OBJECTIVE_COMPLETE = NO
VALID_HUMAN_GATE = NO
GENUINE_BLOCKED = NO
    => VOLUNTARY_WORKFLOW_TERMINATION = FORBIDDEN
```

Any ordinary `FAIL` is new evidence about the current attempt or route. It is
not, by itself, an automatic termination reason.

## 3. Core Principle

`NO_ACTION_CURRENTLY_ALLOWED` is not the same as
`NO_LEGAL_AUTOMATED_PATH_EXISTS`.

For example, `REQUEST_EXECUTION` may be rejected because the current
iteration attempt budget is exhausted. That rejection does not prove that the
Stage cannot continue. The supervisor must query canonical authority and look
for a legal transition such as:

`ADVANCE_ITERATION`, `ASSESS_RESULT`, `REPLAN`, `TECHNICAL_RECOVERY`,
`GPT_ESCALATION`, or an authorized configuration migration.

The next action must come from existing contracts and authorities. It may not
be guessed from a display message.

## 4. Allowed Termination Conditions

Voluntary autonomous termination is allowed only for one of the following
conditions.

### A. Objective complete

The canonical projection proves an objective completion state such as
`STAGE_READY`, `CLOSED`, or an accepted milestone.

### B. Valid Human Gate

The canonical Human Gate contract must contain all of:

- `QUESTION_FOR_HUMAN`
- `WHY_AI_CANNOT_DECIDE`
- `OPTIONS`
- `CONSEQUENCE`
- `HUMAN_DECISION_REQUIRED = YES`

The gate must represent an irreducible business, semantic, visual, policy, or
authorization decision. Technical uncertainty, ordinary budget exhaustion,
test failure, provider failure, and lifecycle guards are not Human Gates.

### C. Genuine blocked

The blocker record must prove all of the following:

- the blocker is still true;
- bounded automatic recovery is exhausted;
- technical GPT escalation completed;
- no canonical lifecycle alternative exists;
- no authorized technical alternative exists;
- `NO_LEGAL_AUTOMATED_NEXT_ACTION = YES`.

Examples include a required private credential or inaccessible private input,
an irreversible authorization that the user owns, a true external outage with
no authorized alternative, or an explicit Human-owned policy that prevents
continuation.

Everything else, including intermediate technical `FAIL`, must remain inside
the supervisory loop.

## 5. Outer Supervisory Loop

The outer loop is supervisory orchestration over the existing lifecycle:

```python
while objective_not_complete:
    projection = derive_current_projection()

    if objective_complete(projection):
        terminate_successfully()

    if validated_human_gate(projection):
        yield_to_human()

    if validated_genuine_blocker(projection):
        terminate_blocked()

    next_action = derive_legal_next_action()

    if next_action exists:
        execute(next_action)
        observe()
        continue

    reason = explain_why_no_action()

    if reason is technical:
        bounded_recover()
        continue

    if reason is lifecycle_guard:
        solve_for_alternative_legal_transition()
        continue

    if reason is stale_state:
        revalidate_and_reassess()
        continue

    if reason is configuration_or_policy:
        inspect_authority()
        migrate_or_repair_if_authorized()
        continue

    if route_is_uncertain:
        automatic_gpt_technical_escalation()
        apply_decision()
        continue

    if human_only_decision_is_proven:
        yield_to_human()

    if no_legal_automated_path_is_proven:
        genuine_blocked()
```

`FAIL` is never the loop's termination condition. Every continuation is
bounded by attempt, iteration, total Stage, dependency, revalidation, and
validator budgets plus the canonical iteration ceiling.

## 6. Layer Responsibilities

### Execution Provider

Codex, Luna, Astra, or another executor performs implementation, commands,
tests, artifacts, and experiment execution. It reports immutable evidence and
does not own lifecycle transitions.

### GPT Technical Reviewer

The technical reviewer diagnoses failures, interprets evidence, selects a
technical route, proposes typed `REPLAN`, and authorizes `CONTINUE` when the
existing contract makes the route legal. It does not own the journal.

### Workflow Supervisor

The Workflow runtime and canonical controller own lifecycle, classification,
recovery, routing, automatic GPT invocation, decision application, legal
next-action search, budget-authority inspection, and termination validation.

### Human

Human owns requirements, business or semantic choices, visual or semantic
acceptance, private inputs, explicit authorization, and irreversible actions.
Human is not a technical message relay.

## 7. FAIL Is Evidence, Not Completion

The following are default `INTERMEDIATE_EVIDENCE` classifications:

`TEST_FAILED`, `PROVIDER_TIMEOUT`, `RELEASE_GATE_FAILED`,
`ITERATION_ATTEMPT_EXHAUSTED`, `CONFIGURATION_MISMATCH`,
`MISSING_STAGE_OWNED_ARTIFACT`, `SCHEMA_FAILURE`, `ALGORITHM_FAILURE`, and
`IMPLEMENTATION_FAILURE`.

They may change the current projection, release an attempt through a canonical
revalidation, trigger bounded recovery, or trigger technical GPT escalation.
They cannot directly end the autonomous objective.

## 8. Lifecycle Guard Resolution

When the controller rejects an action, the supervisor must not execute
`DENIED -> report -> stop`. It must:

1. classify the guard;
2. inspect the canonical projection;
3. query legal alternatives, decision authority, budget authority, dependency
   status, and blocker validity;
4. derive and execute the next legal transition;
5. observe the result and repeat the bounded loop.

For example, if `REQUEST_EXECUTION` is denied for
`PER_ITERATION_ATTEMPT_BUDGET_EXHAUSTED`, and the latest applicable technical
decision is `CONTINUE`, the Stage is active, the objective is unchanged, and
the next iteration is allowed, the legal route is:

```text
ADVANCE_ITERATION
    -> new semantic iteration
    -> fresh bounded iteration scope
    -> REQUEST_EXECUTION
```

The existing canonical `StageController.advance_iteration` command remains
the only lifecycle transition authority.

## 9. Budget Semantics

Budget dimensions are separate:

- per-attempt boundary: one durable provider execution attempt and its
  exactly-once handoff/evidence;
- per-iteration boundary: attempts allowed in one semantic iteration;
- total Stage/global boundary: all budgeted attempts for the Stage;
- iteration ceiling: the maximum number of semantic iterations;
- related dependency, descendant, revalidation, and validator boundaries.

Budgets are safety boundaries, not a shortcut to Human escalation. When a
local scope is exhausted, the supervisor tries the canonical next scope. When
the global scope is exhausted, it inspects the authority and origin before
concluding that continuation is impossible.

Budget provenance must distinguish:

`explicit Human-authored cap`, `current execution-profile policy`,
`legacy/default cap`, `stale migrated configuration`, and other authoritative
origins. A legacy/default value that conflicts with the current
`autonomous_research` policy may be repaired only through canonical migration;
history is preserved and the journal is never edited by hand. An explicit
Human-owned immutable cap must not be silently overridden and can become a
Human Gate only when the required authorization is proven.

### Ownership before genuine blocked

Missing artifacts and capabilities are not genuine blockers by label alone.
Before a new `BLOCKED` can authorize termination, the controller validates an
append-only ownership proof for every missing item. `STAGE_OWNED_WORK`,
`PROVIDER_IMPLEMENTABLE`, `GPT_DESIGNABLE`, and other automated routes force
technical continuation; `UNKNOWN` forces technical review and cannot validate
genuine blocked. A proven misclassification preserves the old blocker and
adds a `MISCLASSIFIED_STAGE_OWNED_WORK` revalidation. That revalidation can
unlock at most two bounded `INFRASTRUCTURE_REPAIR` attempts for capability
creation/execution; these attempts do not change the ordinary per-iteration or
total scientific attempt budgets and cannot create an iteration or a second
lifecycle.

The implementation must remain bounded. It must not replace a finite budget
with `100`, `unlimited`, or a disabled safety cap.

## 10. Context Compression Recovery Contract

At the beginning of every new Codex session, context compaction, `resume`, or
maintenance continuation, the recovery order is:

1. read this document;
2. read current Git `HEAD` and status;
3. read the current journal and canonical projection;
4. read the latest relevant provider and GPT receipts;
5. reconstruct the unfinished objective and its identity;
6. continue from the canonical next action under this design.

### CONTEXT_RECOVERY_CHECKLIST

- [ ] Read `AUTONOMOUS_OBJECTIVE_COMPLETION_LOOP.md` completely.
- [ ] Confirm current branch, commit, status, and immutable tags.
- [ ] Confirm `StageController` journal revision and current projection.
- [ ] Confirm project, Stage, objective, semantic iteration, assessment, and
      attempt identities.
- [ ] Read the latest provider handoff, observation, assessment, GPT decision,
      and technical consultation receipts.
- [ ] Recompute counted attempts from canonical authority; do not trust a
      display summary.
- [ ] Inspect execution profile and budget authority/origin.
- [ ] Validate Human Gate and genuine-blocked evidence before yielding or
      terminating.
- [ ] Derive the next legal canonical action.
- [ ] Resume provider execution or automatic technical escalation as required.
- [ ] Record durable evidence only through existing Workflow commands.

Chat history is never the recovery authority.

## 11. No Second Lifecycle

Do not create `AutonomousSupervisorController`, `RecoveryLifecycle`,
`BlockedController`, a second journal, or a second Stage state machine.

`StageController` remains the sole lifecycle authority. The Outer Loop is
supervisory orchestration that queries and invokes existing canonical
transitions. It must preserve exactly-once command semantics, immutable
journal history, objective binding, Human Gate validation, and bounded
execution.

## 12. Human Message Relay Invariant

The invariant is:

`HUMAN_MUST_NOT_BE_TECHNICAL_MESSAGE_RELAY`

The normal technical route is:

```text
Codex/provider error
    -> Workflow evidence and classification
    -> automatic GPT technical review when needed
    -> Workflow decision application
    -> Codex/provider or canonical lifecycle action
```

The Human may still be the owner of a genuine business decision, private
input, or irreversible authorization. Those are not technical relay steps.

## 13. Current Regression Cases

The maintenance suite must preserve and document these historical cases:

### A. Stale technical BLOCKED after infrastructure repair

The old technical blocker is revalidated without relaying the problem through
Human. Historical evidence stays immutable and a bounded fresh route is
opened only through the canonical controller.

### B. Missing Stage-owned input mistaken for a blocker

A missing artifact owned by the Stage is `WORK_REMAINING`, not an external
Human-only blocker. The provider is allowed to generate it through the
bounded technical route.

### C. GPT CONTINUE applied while the iteration attempt is exhausted

An applied, objective-bound `CONTINUE` must use canonical
`ADVANCE_ITERATION` when the next iteration is legal, then request execution
in the new bounded scope. It must not direct-run a request in the exhausted
iteration.

### D. v2.1.4 candidate with facade global budget 1/1 exhausted

The candidate repaired per-iteration continuation, but the real facade can
still reach `1/1` total attempts and emit a maintenance `FAIL` without
continuing. This is the primary regression for this closure. The correct route
is:

```text
global budget exhausted
    -> inspect budget authority and origin
    -> canonical migration or policy repair when authorized
    -> otherwise automatic GPT technical escalation
    -> apply the typed decision
    -> continue or yield only under the termination validator
```

An ordinary report-and-stop is not acceptable.

### E. Facade S4 ownership-classification gap

The facade's old `GENUINE_BLOCKED` record had no ownership proof. The current
audit identifies the missing S4-S7 scenes as Stage-owned work and the missing
scanline/incidence/occlusion/opening routes as provider-implementable work.
The old record remains immutable history, while the current route is
revalidated and resumed automatically. The S4 bounded implementation and
in-memory generator/extractor/evaluator may be created and executed, but a
synthetic capability result is not benchmark or scientific success evidence.

## 14. Implementation Plan

- Phase A: audit every termination path and produce the internal termination
  matrix.
- Phase B: implement or extract one unified termination validator and wire it
  into runtime/report/release boundaries.
- Phase C: integrate legal-next-action resolution with existing contracts and
  canonical controller commands.
- Phase D: integrate budget authority/origin inspection and canonical migration
  without bypassing explicit Human-owned caps.
- Phase E: integrate context/resume reconstruction from the durable design
  document, journal, projection, and receipts.
- Phase F: add termination, Human relay, budget, Human Gate, genuine-blocked,
  and context recovery regressions.
- Phase G: run clean-room validation: fresh checkout, install, setup, doctor,
  resume, secret scan, and regression suites.
- Phase H: run the real facade E2E from its current unfinished objective using
  the canonical `workflow resume` facade.
- Phase I: promote only after critical gates, no-new-regression evidence, and
  bounded safety pass; otherwise create the convention-compatible immutable
  candidate and do not create or move a stable tag.

## 15. Acceptance Criteria

The implementation is accepted only when the following are evidenced:

```text
UNFINISHED_OBJECTIVE_WITHOUT_HUMAN_BLOCKER_MUST_NOT_TERMINATE = PASS
HUMAN_MUST_NOT_BE_TECHNICAL_MESSAGE_RELAY = PASS
FAIL_IS_INTERMEDIATE_EVIDENCE = PASS
LIFECYCLE_GUARD_RECOVERY = PASS
AUTOMATIC_GPT_TECHNICAL_ESCALATION = PASS
GPT_CONTINUE_AUTO_APPLY = PASS
GPT_REPLAN_AUTO_APPLY = PASS
BUDGET_AUTHORITY_HANDLING = PASS
CONTEXT_COMPACTION_RECOVERY = PASS
SECOND_LIFECYCLE_CREATED = NO
```

## 16. Termination-Path Audit Matrix

This matrix is the required audit artifact. The implementation phase must
replace any `TO_VERIFY` entry with a concrete code path and evidence index.

| TERMINATION_PATH | OBJECTIVE_COMPLETE_REQUIRED? | HUMAN_GATE_REQUIRED? | GENUINE_BLOCKED_REQUIRED? | CURRENT_BEHAVIOR | DESIRED_BEHAVIOR |
|---|---:|---:|---:|---|---|
| provider `FAIL`/timeout | NO in legacy path | NO | NO | may surface failure and end facade turn | record evidence, classify, recover/escalate, continue |
| `REQUEST_EXECUTION` lifecycle guard | NO | NO | NO | controller rejects action | classify guard and derive canonical alternative |
| per-iteration exhausted | NO | NO | NO | candidate handles only authorized continuation | search next iteration or technical recovery |
| total budget exhausted | NO | NO | NO | facade may report `FAIL` and stop | inspect authority/origin, migrate if authorized, escalate if needed |
| release gate failure | NO | NO | NO | wrapper may return release failure | classify recoverable cause and rerun relevant E2E |
| stale blocker | NO | NO | NO | stale state can project `BLOCKED` | revalidate and reassess automatically |
| missing Stage-owned artifact | NO | NO | NO | may look blocked | classify as work remaining and execute |
| technical GPT uncertainty | NO | NO | NO | may end turn | automatic GPT escalation and apply typed decision |
| valid Human Gate | NO | YES | NO | yields to Human | yield with complete gate contract |
| genuine external/private blocker | NO | NO | YES | may be blocked | yield/terminate only with proof of no legal path |
| completed Stage/objective | YES | NO | NO | success/closeout | terminate successfully |

The matrix is not a second authority; it is a review index for the existing
authority chain.

## 17. Legal Next-Action Resolution Contract

For a rejected or currently unavailable action, the resolver must consume:

- current canonical projection;
- legal actions from the existing contract;
- decision authority and the latest objective-bound assessment;
- budget authority and counted attempt evidence;
- dependency state;
- blocker validity and recoverability;
- current Stage and semantic iteration identity.

It returns either one exact existing canonical action with its evidence and
preconditions, or an explanation of why no legal action remains. The resolver
must not invent action names, silently mutate the journal, retry the same
failure forever, or convert a technical guard into a Human Gate.

## 18. Regression Test Inventory

### Termination contract

- `test_unfinished_objective_without_human_blocker_cannot_terminate`
- `test_intermediate_failure_is_evidence_not_terminal`
- `test_failed_release_gate_with_recoverable_cause_continues`
- `test_no_current_action_does_not_mean_no_legal_path`
- `test_lifecycle_guard_triggers_alternative_action_resolution`
- `test_technical_problem_does_not_end_outer_supervisor`
- `test_genuine_human_gate_can_terminate`
- `test_genuine_external_blocker_can_terminate`
- `test_completed_objective_can_terminate`

### Human relay

- `test_human_is_not_required_between_provider_and_gpt`
- `test_human_is_not_required_to_apply_gpt_continue`
- `test_human_is_not_required_to_apply_gpt_replan`
- `test_human_is_not_required_to_request_next_iteration`
- `test_human_is_not_required_to_recover_stale_technical_blocker`

### Budget

- `test_pre_dispatch_failure_does_not_consume_provider_attempt`
- `test_real_provider_execution_consumes_attempt`
- `test_per_iteration_exhaustion_auto_advances_when_authorized`
- `test_legacy_budget_can_be_canonically_migrated`
- `test_explicit_human_budget_cap_is_not_silently_overridden`
- `test_global_budget_exhaustion_triggers_technical_resolution_before_human`
- `test_budget_recovery_remains_bounded`
- `test_same_failure_cannot_create_infinite_iterations`

### Context recovery

- `test_resume_reconstructs_unfinished_objective`
- `test_resume_reloads_outer_loop_contract_semantics`
- `test_stale_fail_report_does_not_control_current_projection`
- `test_context_recovery_continues_from_next_legal_action`

### Genuine Human Gate

Construct two technically valid routes whose difference is a final business
semantic choice. Evidence must not decide between them, so the Human Gate is
legal. The same suite must prove ordinary budget/config/test/provider failures
do not trigger that gate.

### Genuine blocked

Construct a Stage requiring an explicitly private external input that is absent,
cannot be generated by the repository, is not authorized for automatic
retrieval, and has no GPT-confirmed alternative. Only then may the projection
become genuine `BLOCKED`.

## 19. Real Facade E2E Contract

The real business project is resumed from its current durable state via the
canonical installed Workflow command. The facade must not be re-bootstrapped,
its journal must not be edited by hand, and a duplicate Stage or objective must
not be created.

Required evidence fields are:

```text
FACADE_STAGE_RESUMED = YES
HUMAN_MESSAGE_RELAY_REQUIRED = NO
LEGAL_NEXT_ACTION_AUTOMATICALLY_DERIVED = YES
NEW_ITERATION_STARTED = YES when lifecycle requires it
PROVIDER_EXECUTION_AUTHORIZED = YES
S4_GENERATION_STARTED = YES when the recovered route reaches S4
```

For the facade regression, `S4_GENERATION_STARTED` is an explicit provider
claim backed by the generator route and its generated-input/evidence markers;
the presence of a runner receipt alone is not sufficient.

After S4 starts, an ordinary timeout, implementation failure, generator bug,
test failure, or missing Stage-owned evaluator remains intermediate evidence.
The loop must classify, recover, escalate, replan, and execute until a valid
termination condition is reached. This Engine release does not need to finish
all S4-S7 scientific work; it must hand the facade back into its normal
`autonomous_research` execution profile.

## 20. Clean-room and Release Rules

The release evidence must cover focused regressions, full regression,
compile, diff check, secret scan, fresh checkout, install, setup, doctor,
resume, outer-loop regressions, budget regressions, automatic technical
escalation, Human Gate validation, and genuine-blocked validation.

Known historical fixture failures may be reported as
`PASS_WITH_BASELINE_LIMITATION` only when the same baseline failure is proven
and there are no new regressions.

Never move or overwrite `workflow-v2.1.3-stable` or the existing
`workflow-v2.1.4-release-candidate`. If new commits are made after the current
candidate, create the next immutable candidate using the repository's naming
convention, for example `workflow-v2.1.4-release-candidate-r2`.

Create `workflow-v2.1.4-stable` only after all critical gates pass. Stable
promotion depends on correct handling of recoverable failures, the termination
contract, no Human technical relay, bounded safety, clean-room evidence, and
no new regressions. It does not require that no intermediate failure ever
occurred.

## 21. Stop Rule

After the docs-only commit, this maintenance task may end only when:

1. the maintenance objective is complete, implementation, tests, real E2E,
   and release promotion are complete; or
2. a valid Human Gate is proven and states why AI cannot decide; or
3. a genuine blocker is proven with `NO_LEGAL_AUTOMATED_NEXT_ACTION = YES`.

The following are not valid stop reasons: test failure, provider timeout,
release gate failure, iteration exhaustion, budget exhaustion, action denied,
schema failure, an unpromotable candidate, implementation uncertainty,
technical route uncertainty, need for GPT, or configuration/migration work.
Each requires the next bounded diagnostic or recovery action.

## 22. Durable Final Evidence

After implementation, reopen this document and update the compact evidence
index below. Do not paste large logs into the document.

```text
IMPLEMENTED_IN_COMMIT: b02d761 (ownership implementation; docs-first commit 7132780; prior implementation 7d27e1c; terminal budget review 6b76fd1)
TEST_EVIDENCE: focused ownership/S4 suite 32 passed; compileall passed; full suite excluding historical self-test fixture 635 passed, 1 skipped; full suite 635 passed, 1 skipped, 2 proven baseline fixture failures
REAL_E2E_EVIDENCE: facade journal revision 43 preserved the old blocker append-only, invalidated its termination authority, completed two bounded INFRASTRUCTURE_REPAIR attempts, and observed attempt 8 with explicit s4_generation_started=true; clean-room PASS at D:/work/workflow-v2.1.4-release-validation-20260915-r5-final.json from candidate r5
FINAL_INVARIANTS: PASS; unfinished objectives do not terminate for ordinary technical failure; ownership is required before genuine blocked; termination is validator-gated; no Human technical relay; facade human_intervention_count=0; ordinary budget remains max_iterations=2/max_attempts_per_iteration=1/max_attempts_total=2
KNOWN_LIMITATIONS: historical self-test fixture is missing .consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json; facade S5-S7 scientific work remains incomplete and no scientific success is inferred from the bounded S4 marker
CURRENT_STABLE_TAG: workflow-v2.1.4-stable
```

The final Human summary must answer, in ordinary Chinese:

1. why the previous Codex turn could stop before objective completion;
2. what the new Outer Supervisory Loop does;
3. whether `UNFINISHED_OBJECTIVE_WITHOUT_HUMAN_BLOCKER_MUST_NOT_TERMINATE` is
   implemented;
4. what happens after intermediate `FAIL`;
5. what happens after a rejected lifecycle action;
6. whether Workflow searches for another legal action;
7. whether uncertain technical routes trigger automatic GPT;
8. whether GPT `CONTINUE`/`REPLAN` apply automatically;
9. how exhausted budgets are handled;
10. where the facade's original 1/1 budget came from;
11. whether bounded safety remains;
12. whether Human remains a technical relay;
13. when Human is legitimately requested;
14. how context compression recovery works;
15. whether a second lifecycle was created;
16. whether the facade resumed automatically;
17. whether S4 entered scientific execution;
18. the Human intervention count;
19. the stable tag;
20. whether Workflow returned to Maintenance Mode.

The machine-facing report must include:

```text
DESIGN_DOCUMENT:
DESIGN_DOCUMENT_COMMIT:
FINAL_HEAD:
OUTER_SUPERVISORY_LOOP:
UNFINISHED_OBJECTIVE_NON_TERMINATION:
FAIL_IS_INTERMEDIATE_EVIDENCE:
TERMINATION_VALIDATOR:
LEGAL_NEXT_ACTION_RESOLUTION:
LIFECYCLE_GUARD_RECOVERY:
BLOCKER_CLASSIFICATION:
AUTOMATIC_GPT_TECHNICAL_ESCALATION:
GPT_CONTINUE_AUTO_APPLY:
GPT_REPLAN_AUTO_APPLY:
HUMAN_GATE_VALIDATOR:
GENUINE_BLOCKED_VALIDATOR:
HUMAN_MESSAGE_RELAY_REQUIRED:
CONTEXT_COMPACTION_RECOVERY:
SECOND_LIFECYCLE_CREATED:
BUDGET_AUTHORITY_AUDITED:
FACADE_1_1_BUDGET_ORIGIN:
BUDGET_MIGRATION_REQUIRED:
BOUNDED_SAFETY_PRESERVED:
PER_ITERATION_LIMIT:
GLOBAL_LIMIT:
UNBOUNDED_RETRY_POSSIBLE:
FACADE_PROJECT_ID:
FACADE_STAGE_ID:
FACADE_STAGE_RESUMED:
LEGAL_NEXT_ACTION_AUTOMATICALLY_DERIVED:
NEW_ITERATION_STARTED:
S4_GENERATION_STARTED:
SCIENTIFIC_EXECUTION_RESUMED:
HUMAN_INTERVENTION_COUNT:
FOCUSED_TESTS:
FULL_REGRESSION:
NO_NEW_REGRESSIONS:
CLEANROOM:
FINAL_COMMIT:
NEW_RELEASE_CANDIDATE:
NEW_STABLE_TAG:
KNOWN_LIMITATIONS:
WORKFLOW_PRODUCT_MODE:
NEXT_BUSINESS_ACTION:
```

## 23. Final Success Marker

The final marker may be `AUTONOMOUS_OBJECTIVE_COMPLETION_LOOP: PASS` only when
unfinished objectives no longer stop for ordinary technical states, Human is
not a technical relay, legal actions are automatically derived, technical
uncertainty invokes GPT automatically, GPT decisions apply automatically,
bounded safety remains, the real facade resumes from its current durable
state, clean-room validation passes, and there are no new regressions.

If the only limitation is a proven historical baseline fixture,
`PASS_WITH_BASELINE_LIMITATION` is allowed. If an ordinary technical `FAIL`
still leads to a final report that requires Human to tell the system what to do
next, the marker is `FAIL`; that failure is itself evidence requiring further
work whenever a legal automated path remains.

## 24. Target Final State

After successful closure:

```text
Workflow -> STABLE -> MAINTENANCE MODE
```

Human defines requirements, makes genuine business decisions, and performs
final visual or semantic acceptance. Workflow, Codex, and GPT execute,
diagnose, recover, test, reason technically, replan, transition lifecycle, and
handle bounded budgets. Human owns the goal and acceptance, not the act of
keeping the AI moving.

## 25. Bounded Project Plan Ingestion

The user-facing planning contract is documented separately in
`docs/PROJECT_PLAN_INGESTION_CONTRACT.md`. A project may provide only
`plan/REQUIREMENTS.md` and `plan/STAGE_PLAN.md`; Workflow derives
`plan/WORKFLOW_PLAN.md` and `plan/CURRENT_STATE.md` and keeps the canonical
`StageController + journal + contracts` authority unchanged. The plan layer
does not create a second lifecycle, registry, manifest, or project state
store. On every entry and resume it rechecks the fixed paths, preserves the
legacy no-plan path, reloads plan sources before the journal projection, and
regenerates missing or conflicting derived views from the journal.

Plan-owned missing inputs remain work remaining and are eligible for the
existing bounded execution route. Existing journal Stages are aligned without
restarting closed work; future-only plan changes update the derived view, while
current, completed, accepted, or final-objective changes are marked for
technical/plan review without rewriting history. Normal Stage completion and
Human approval refresh the snapshot and select the next planned Stage through
the existing controller.

### 25.1 Bounded Project Plan Ingestion Release Evidence

```text
IMPLEMENTED_IN_COMMIT: 1f9ae89 (plan ingestion implementation 502b1d3; CLI missing-source boundary fix 1f9ae89)
PLAN_CONTRACT: docs/PROJECT_PLAN_INGESTION_CONTRACT.md
PLAN_TEST_EVIDENCE: 41 focused plan/runtime tests passed; plan E2E subset 5 passed
FULL_REGRESSION: 660 passed, 1 skipped, 2 deselected known historical self-test fixture failures
PORTABLE_VALIDATION: PASS; destructive_actions=0; resume_idempotence=YES; frozen_kernel_unchanged=YES
DOCUMENTATION_AUDIT: PASS
CLEANROOM: FAIL_WITH_EXTERNAL_REVIEW_LIMITATION; first bounded run stopped at ATTACHMENT_UPLOAD_FAILED before prompt; one controlled retry uploaded attachments and stopped at GPT_DECISION_INVALID during planning review; current product worktree writes=NO
CURRENT_RELEASE_CANDIDATE: workflow-v2.1.5-release-candidate-r2
CURRENT_STABLE_TAG: NOT_PROMOTED; stable promotion requires clean-room PASS
KNOWN_LIMITATIONS: historical self-test fixture is missing .consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json; the external bridge/GPT decision contract did not provide clean-room release evidence
NEXT_ACTION: retry clean-room only after the external bridge/GPT response state changes; no Human technical relay is required
```
