# Spec Kit Adoption & Repository Architecture V1 — Tasks

Task set version: `1`  
Spec reference: `87a564b1674064c74039827320782a03a17d30ea506acc1f0cf98c1318c83058`  
Plan reference: `e7967eddade69067f74ff0273a46c1bc2adb1d443366fd2acf4946c7a1c17f23`

## T001 — reusable templates

- Paths: `templates/`
- Requirement refs: `FR-001`, `FR-002`
- Depends on: none
- Completion: constitution, spec, plan, tasks, and CLOSEOUT templates render
  deterministically without machine paths or concrete Stage identity.

## T002 — binding and scope validator

- Paths: `src/guidance_artifacts.py`
- Requirement refs: `FR-003`, `FR-004`
- Depends on: T001
- Completion: accepted-intent, spec/plan/task references, stable IDs, and
  allowed-path checks fail closed.

## T003 — representative bundle

- Paths: `specs/spec-kit-adoption-and-repository-architecture-v1/`
- Requirement refs: `FR-001`, `FR-002`, `FR-005`
- Depends on: T001, T002
- Completion: one readable bundle retains old guidance, binds the accepted
  intent, and adds no lifecycle authority.

## T004 — derived projections and E2E

- Paths: `src/guidance_artifacts.py`, `tests/test_guidance_artifacts.py`
- Requirement refs: `FR-003`, `FR-004`
- Depends on: T002, T003
- Completion: CLOSEOUT and PROJECT_HANDOFF derive from canonical state; the
  representative Stage completes the real V2 planning, execution, assessment,
  GPT review, integration, and closeout path.

Task completion is evidence for the Stage. It never independently marks a
Stage successful.
