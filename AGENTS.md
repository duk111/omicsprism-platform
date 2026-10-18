
# OmicsPrism Platform — Coding Guidelines

## Goal
Improve reliability and usability incrementally.
Reuse existing components before adding abstractions.

## Architecture
- omicsprism: scientific computation engine
- omicsprism-platform: FastAPI, React, jobs, and LangGraph Copilot
- Keep DEG, DEM, and GMA independently executable.
- Do not introduce automatic multi-analysis orchestration.

## Scientific correctness
- Do not change scientific algorithms without explicit approval.
- Never silently drop an exposed analysis parameter.
- Preserve valid 0, False, and explicitly provided values.
- Do not invent preprocessing state or infer it from filenames.
- Verify defaults against the actual scientific engine.
- Require regression tests for parameter mapping changes.

## Implementation
- Reuse existing Pydantic models, API routes, and React components.
- Prefer small, focused changes over broad refactoring.
- Preserve ownership checks, idempotency, and task persistence.
- Avoid new dependencies unless clearly justified.
- Do not add database migrations without approval.

## Workflow
1. Inspect relevant code and tests.
2. Explain the root cause and smallest safe solution.
3. Add a failing regression test where practical.
4. Implement the minimal change.
5. Run affected tests and builds.
6. Review the diff for unrelated modifications.
7. Report changed files, test evidence, and remaining risks.

## Boundaries
- Never change production data or secrets.
- Never claim unexecuted tests have passed.
- Ask for scientific decisions rather than guessing.