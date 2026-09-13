# Spec Kit Adoption & Repository Architecture V1 — Plan

Plan version: `1`  
Spec reference: `87a564b1674064c74039827320782a03a17d30ea506acc1f0cf98c1318c83058`

## Technical approach

Keep the existing V2 contracts and controller unchanged. Store reusable
templates under `templates/`, validate bindings in the Engine helper, and put
this representative guidance bundle under the project `specs/` root. Render
closeout and handoff from the canonical journal projection.

## Research rationale and tradeoffs

Official Spec Kit guidance separates intent, design, and executable tasks while
allowing Flow-forward history. The adapter keeps that separation but rejects
Spec Kit hooks as a lifecycle owner. The first bundle is deliberately narrow so
old guidance remains readable and future migration can be reviewed separately.

## Allowed scope

`templates/`, `src/guidance_artifacts.py`, `tests/test_guidance_artifacts.py`,
and `specs/spec-kit-adoption-and-repository-architecture-v1/`.

## Verification strategy

Render each template twice and compare bytes. Verify the spec digest against the
approved brief, bind plan to spec and tasks to plan, reject paths outside the
Stage scope, require stable task IDs, and reject a hand-written successful
closeout until the canonical stage is `CLOSED`. Regenerate PROJECT_HANDOFF from
the frozen projection and run the existing V2 regression suite plus the new
guidance tests.
