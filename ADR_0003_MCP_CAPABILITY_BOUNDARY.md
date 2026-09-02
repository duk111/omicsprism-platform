# ADR 0003: MCP Uses the Shared Capability Boundary

## Status

Accepted for Phase 7.2. The shared registry and an in-process MCP adapter are
implemented; remote MCP transport is not enabled.

## Context

The Web Agent already owns bounded, typed read operations in
`AgentToolRuntime`. An MCP adapter must not call FastAPI routes or duplicate
the storage, ownership, artifact, or evidence logic. A protocol adapter must
also preserve strict request schemas: generic SDK argument models may ignore
unknown object keys unless the application validates them first.

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

The registry is instantiated for an ownership-scoped runtime. The
`mcp==2.1.1` official Python SDK is used by `mcp_adapter.py` to build an
in-process `MCPServer` with six typed, structured-output tools. The adapter
binds a trusted `CapabilityPrincipal` at construction, derives its callable
signatures from the registry models, and validates raw arguments through the
registry before delegating result conversion to the SDK. No tool accepts a
principal or user id as a client argument. Remote MCP listening remains
disabled until authentication, quota, rate-limit, audit, and trace integration
are reviewed.

## Consequences

- Web Agent and MCP cannot silently diverge in read-only behavior.
- Unknown and cross-principal capability access does not reveal resource
  existence.
- The adapter can be exercised in-process without opening a port or changing
  deployment topology.
- MCP transport and authorization remain an explicit later change rather than
  an undocumented protocol implementation.
- `prepare_analysis` and `submit_analysis` are intentionally not registered;
  no MCP path can create a Job in this phase.
