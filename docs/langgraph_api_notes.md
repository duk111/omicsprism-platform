# LangGraph API Notes

This note records the API installed and inspected locally for Phase 4. Later
Phase 4 tasks must use these APIs directly and must not add multi-version
compatibility branches.

## Installed versions

- Python: `3.13.7`
- `langgraph==1.2.11`
- `langgraph-checkpoint==4.2.0`

`langgraph` 1.2.11 has no top-level `__version__` attribute. The installed
versions were confirmed with:

```python
from importlib.metadata import version

assert version("langgraph") == "1.2.11"
assert version("langgraph-checkpoint") == "4.2.0"
```

The Postgres checkpointer extension is not installed. Phase 4.7 selected the
documented `InMemorySaver` fallback for the reasons recorded below.

## Graph definition and compilation

The inspected `StateGraph` API uses a state schema at construction, explicit
nodes and edges, and `compile(checkpointer=...)`:

```python
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    answer: str


builder = StateGraph(State)
builder.add_node("node_name", node_function)
builder.add_edge(START, "node_name")
builder.add_edge("node_name", END)
checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)
```

Relevant installed signatures:

```text
StateGraph(state_schema, context_schema=None, *, input_schema=None, output_schema=None, ...)
StateGraph.add_node(self, node, action=None, *, ...)
StateGraph.add_edge(self, start_key, end_key)
StateGraph.compile(self, checkpointer=None, *, cache=None, store=None,
                   interrupt_before=None, interrupt_after=None, ...)
```

Although `compile()` still exposes static `interrupt_before` and
`interrupt_after` arguments, Phase 4 HITL must use the resumable runtime
`interrupt()` API described below.

## Interrupt and resume

The installed runtime API is `langgraph.types.interrupt(value)`. The first call
inside a node pauses execution and surfaces the value under `__interrupt__`.
Execution resumes with `Command(resume=...)`; the same checkpointer and the same
`configurable.thread_id` are required. The interrupted node restarts from its
beginning, so code before `interrupt()` must be deterministic and safe to run
again.

```python
from langgraph.types import Command, interrupt


def ask(state: State) -> State:
    answer = interrupt({"kind": "clarification", "question": "Choose"})
    return {"answer": str(answer)}


config = {"configurable": {"thread_id": "thread-123"}}
paused = graph.invoke({}, config)
resumed = graph.invoke(Command(resume="selected"), config)
```

Relevant installed signatures:

```text
interrupt(value: Any) -> Any
Command(*, graph=None, update=None, resume=None, goto=())
Compiled graph invoke(input: Input | Command | None, config=None, ...)
```

A local smoke invocation produced an `Interrupt` containing the supplied value,
then resumed to `{'answer': 'selected'}`. This confirms that the architecture's
runtime `interrupt()` plus `Command(resume=...)` description matches the
installed generation of LangGraph.

## In-memory checkpointer lifecycle

The installed in-memory implementation is
`langgraph.checkpoint.memory.InMemorySaver`. `MemorySaver` is only an alias for
that class. Its constructor is:

```text
InMemorySaver(*, serde=None, factory=collections.defaultdict)
```

`InMemorySaver` implements synchronous and asynchronous context managers, but a
context manager is not required for its normal default in-memory storage. Both
direct construction and `with InMemorySaver() as saver:` are supported. The
local smoke check used direct construction and confirmed `get_tuple(config)`
returned the saved checkpoint. Phase 4 code should use the canonical
`InMemorySaver` name.

## Phase 4.7 checkpointer decision

The application currently opens direct `psycopg.connect(...)` connections and
does not have a shared connection pool. The installed environment also lacks
both `langgraph-checkpoint-postgres` and `psycopg_pool`. Adding the Postgres
checkpointer would therefore require another package, pool integration, and
checkpoint schema management instead of lightly reusing the existing stack.

Phase 4.7 consequently compiles each graph instance with one `InMemorySaver` by
default. Callers may still inject a checkpointer directly, without a persistence
abstraction or configuration layer. Interrupt and resume calls must use the same
compiled graph instance and the same `configurable.thread_id`; different thread
IDs have isolated checkpoints.

The accepted limitation is that a process restart loses a turn that is currently
waiting at an interrupt, so the user must initiate that turn again. Existing
thread and message records, submitted Jobs, and produced artifacts remain in
their current business stores and are unaffected. No PlanStore, ApprovalStore,
or custom graph-state database is introduced.

## API state ownership

The Agent API uses the business `thread_id` as
`configurable.thread_id` for every invoke, resume, and state lookup. `focus`
and its monotonic `version` are fields in `GraphState`, so the LangGraph
checkpointer is the sole owner of that conversational state. The public
`checkpoint_turn_id` remains the turn record used for ownership and
idempotency checks; it is not a second checkpoint namespace.

## Local sources inspected

- `.venv/Lib/site-packages/langgraph/graph/state.py`: `StateGraph.compile`
- `.venv/Lib/site-packages/langgraph/types.py`: `Command` and `interrupt`
- `.venv/Lib/site-packages/langgraph/checkpoint/memory/__init__.py`:
  `InMemorySaver` lifecycle and `MemorySaver` alias

The signatures were inspected with `inspect.signature`, and the interrupt/resume
contract was additionally exercised with a local minimal graph.
