# Spec Kit Adoption & Repository Architecture V1

Spec version: `1`  
Accepted intent digest: `1c96e7ccab2e8a5a920781fe5f0c53ac8a1b8218ce2d66065f117abf7fb5463b`  
Project ID: `project-8b081cf16e37a7462d011ed2`  
Brief revision: `2`  
Intent source: `.research/PROJECT_BRIEF.json`

## Why

Workflow V2 should be a portable Engine whose project history and temporary
runtime artifacts have clear owners. Spec Kit guidance should improve the
development grammar without becoming a second execution authority.

## What

Adopt or adapt constitution, specification, plan, task, analyze, and converge
guidance. Separate Engine, Project, and Machine-local Runtime roots. Preserve
accepted Flow-forward history, define conservative cleanup rules, and retain
the V2 lifecycle kernel as the only state authority.

## Acceptance criteria

- FR-001 — Official Spec Kit sources are pinned and their responsibilities are explained.
- FR-002 — Human-facing guidance has clear WHAT/WHY, HOW, tasks, and closeout roles.
- FR-003 — Machine journal, observations, assessments, decisions, and receipts remain
  canonical and bound to their inputs.
- FR-004 — Cleanup is a reviewed dry-run only; no deletion, mass move, or rename occurs.
- FR-005 — Product bootstrap is explicit, portable, and does not copy old runtime state.

## Non-goals

Repository cleanup, implementation-evidence migration, Bridge staging
isolation, packaging redesign, and lifecycle-kernel changes are future work.

## Clarified decisions

The architecture Stage was accepted by Human and reviewed by real GPT. This
representative bundle is the Human-facing projection of that accepted intent;
the machine `PROJECT_BRIEF` and V2 journal remain intact.
