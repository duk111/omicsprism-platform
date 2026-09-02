# OmicsPrism Threat Model

## Scope And Assets

The trust boundary includes an internet-facing browser/API, a cloud host, a
private compute host, PostgreSQL, Redis, MinIO, the local vLLM endpoint, and the
uploaded datasets and analysis artifacts. Primary assets are dataset contents,
artifact contents, user/thread/job ownership, credentials, model prompts, and
the integrity of analysis submissions.

## Trust Boundaries

1. The browser is untrusted input. Session authentication and API ownership
   checks are applied before product-store access.
2. The cloud-to-compute connection is a service boundary. The runtime receives
   only queue items and uses explicit database, Redis, and object-storage
   credentials; vLLM is bound to the compute host and is not browser-facing.
3. Model output is untrusted data. Pydantic model validation, graph action
   checks, tool budgets, and deterministic services decide what may execute.
4. MCP callers are untrusted unless authenticated by a future transport. The
   current adapter is process-local and binds a trusted principal at
   construction; it accepts no caller-supplied user id.

## Threats And Controls

| Threat | Control | Residual risk |
| --- | --- | --- |
| Cross-user Job, dataset, or artifact read | Ownership-bound stores and `CapabilityRegistry`; not-found and unauthorized are non-enumerating | A compromised service credential can still read its database scope |
| Prompt injection in CSV metadata or artifacts | Data is returned as typed facts; model actions are validated by graph and deterministic services | A model may still produce an unhelpful answer; it cannot gain new tool policy |
| Model emits malformed or unsafe action | Strict JSON schema, action-specific Pydantic validators, read-only tool allowlist, bounded loop | Provider-specific structured-output bugs require retry/failure handling |
| Duplicate queue delivery or Job completion | Durable checkpoint, Redis recovery list, idempotency key, outbox reconciliation | External side effects outside the Job store are not made idempotent here |
| Secret leakage through logs/traces/checkpoints | Trace records hashes, identifiers, latency, and result codes; raw prompts, rows, DSNs, and keys are excluded | Application/server access can still expose process memory or configured secrets |
| Artifact path traversal or oversized evidence | Artifact allowlist, checksum binding, byte/row/field limits and truncation | Malicious files can consume bounded CPU/memory within configured limits |
| Unauthorized remote MCP access | No remote MCP listener; adapter requires a bound principal and optional trace context | Enabling remote transport without JWT/OAuth, quotas, and audit would be unsafe |
| Analysis submission without confirmation | Graph HITL approval, plan version/fingerprint checks, idempotent submission | A stolen authenticated session can approve its own valid plan |

## Operational Requirements

- Keep Postgres, Redis, and MinIO ports reachable only from the required hosts
  and firewall rules.
- Keep vLLM on compute-host loopback or a private interface; never expose it
  through the browser or public reverse proxy.
- Rotate service credentials outside the repository and do not place them in
  trace events, checkpoint payloads, or support bundles.
- Review runtime and trace error rates, queue age, and failed-turn codes before
  enabling any new write capability.

## Known Limitations

Remote MCP authentication, quota/rate limiting shared with Web API, and
multi-replica distributed scheduling remain follow-up work. The local vLLM
provider does not supply a price card, so cost is reported as unknown rather
than inferred.
