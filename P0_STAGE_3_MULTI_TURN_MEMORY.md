# P0 Stage 3: Multi-Turn Context and Structured Memory

## Scope

Stage 3 makes the Postgres/LangGraph checkpoint the durable source for an
Agent thread while the API publishes only the newly received turn input. The
runtime merges that input with the current checkpoint, then invokes the
existing graph. No second state machine or alternate graph was introduced.

The prompt context now has five bounded parts: the fixed system/tool contract,
recent message summaries, structured conversation memory, bounded dataset/job
facts, and the working set of recent observations. Raw CSV bytes, credentials,
DSNs, storage keys, ownership identifiers, and unrestricted message history do
not cross the model boundary.

## Changes

- `AgentTurnInput` carries the new message, optional dataset profile references,
  and explicitly changed Job focus ids. `AgentTurnWorkItem.state` remains a
  legacy compatibility path for already queued fixtures.
- `AgentRuntime` loads the checkpoint before a start turn and merges the input.
  It preserves focus, recent Jobs, confirmed analysis parameters, and memory;
  it resets decision, response, validation, tool observations, interrupts, and
  the per-turn step budget. A retry resumes the same checkpoint rather than
  applying the input twice.
- `GraphState.confirmed_params` keeps the last approved analysis parameters
  after `pending_plan` is cleared. Analysis resolution uses those parameters as
  the next-turn prior, enabling follow-up requests such as “use the plan above”.
- `RecentMessages` retains at most eight clipped text messages. Older messages
  are compacted into a deterministic bounded summary. `ConversationMemory`
  records analysis/contrast/scope/parameter decisions, Jobs, preferences,
  unresolved questions, and citation references with versioned hashes and
  explicit truncation flags.
- New attachments replace the current dataset profile set only when supplied;
  an attachment-free turn inherits the valid checkpoint dataset references.
  The active input-bundle reference is checkpointed too: inheritance requires
  the same user and thread plus active, unexpired status. API validation still
  enforces these boundaries for new attachments.

## Verification

Focused regression coverage includes:

- API enqueue payloads use the typed turn input while legacy runtime fixtures
  continue to execute.
- Two consecutive turns on a real LangGraph checkpoint see the persisted user
  and assistant messages, while the new turn starts with a fresh decision,
  response, observations, and step budget.
- Approved confirmation persists `confirmed_params` after the pending plan is
  removed.
- Twelve-message histories produce eight recent messages plus a deterministic
  compacted summary; each message is clipped to its budget.
- Existing clarification, confirmation, ownership, idempotency, result, and
  tool-boundary tests remain green.
- The eight recorded `multi_turn_memory` Eval v2 scenarios are now release-gate
  cases instead of documented non-gating baselines.

Commands:

    .venv\Scripts\python.exe -m pytest backend/tests/test_agent_api.py backend/tests/test_agent_runtime.py backend/tests/test_context_assembler.py backend/tests/test_graph_flow.py backend/tests/test_graph_state.py -q
    .venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .test-tmp\phase3-full
    .venv\Scripts\python.exe scripts/run_agent_eval_v2.py

Final verification passed with `217 passed, 2 skipped` for the backend suite
when using the repository-local temporary directory. Eval v2 remains
deterministic and does not contact a model endpoint.

## Boundaries

The current deterministic compaction does not ask the model to summarize
history. This avoids untrusted metadata/message text becoming instructions and
keeps replay deterministic. Job completion continuation, result explanation
message blocks, and frontend memory display remain later stages.
