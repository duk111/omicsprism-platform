# ADR 0003: MCP Uses the Shared Capability Boundary

## Status

Accepted for Phase 7.1. The registry is implemented; the MCP transport is not
enabled yet.

## Context

The Web Agent already owns bounded, typed read operations in
`AgentToolRuntime`. An MCP adapter must not call FastAPI routes or duplicate
the storage, ownership, artifact, or evidence logic. The project also does not
currently depend on an MCP Python SDK, so a hand-written protocol endpoint
would create an unverified compatibility and security boundary.

## Decision

`CapabilityRegistry` is the shared internal boundary. Each registered
capability has:

- a strict request and response Pydantic schema;
- a stable name and `readonly-tools.v1` schema version;
- an ownership-bound handler;
- one non-enumerating `not_visible` error for unknown or unauthorized access.

Phase 7.1 registers only `describe_metadata`, `enumerate_contrasts`,
`list_jobs`, `get_job`, `describe_artifacts`, and `query_artifact`. The Web
Agent production executor now dispatches these read operations through the
same registry. The principal is supplied by the trusted caller boundary and
is never accepted as a tool argument.

The registry is instantiated for an ownership-scoped runtime. A future MCP
adapter must create that runtime from an authenticated local/service/JWT
principal and must preserve the same request/response models. Remote MCP
listening remains disabled until the official SDK, authentication, quota,
rate-limit, audit, and trace integration are reviewed.

## Consequences

- Web Agent and MCP cannot silently diverge in read-only behavior.
- Unknown and cross-principal capability access does not reveal resource
  existence.
- MCP transport and authorization remain an explicit later change rather than
  an undocumented protocol implementation.
- `prepare_analysis` and `submit_analysis` are intentionally not registered;
  no MCP path can create a Job in this phase.
