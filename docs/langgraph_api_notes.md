# LangGraph API Notes

This note records the API installed and inspected locally for Phase 4. Later
Phase 4 tasks must use these APIs directly and must not add multi-version
compatibility branches.

## Installed versions

- Python: `3.13.7`
- `langgraph==1.2.11`
- `langgraph-checkpoint==4.2.0`
- `langgraph-checkpoint-postgres==3.1.2`
- `psycopg-pool==3.3.1`

`langgraph` 1.2.11 has no top-level `__version__` attribute. The installed
versions were confirmed with:

```python
from importlib.metadata import version

assert version("langgraph") == "1.2.11"
assert version("langgraph-checkpoint") == "4.2.0"
assert version("langgraph-checkpoint-postgres") == "3.1.2"
assert version("psycopg-pool") == "3.3.1"
```

The Postgres checkpointer extension is installed and pinned for the persistent
runtime path. `InMemorySaver` remains a test-only checkpointer.

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

## Postgres checkpointer

The installed extension is `langgraph-checkpoint-postgres==3.1.2`. Its
`PostgresSaver` accepts either a psycopg connection or a `ConnectionPool`.
`setup()` is run by the migration command with the administrator DSN; the API
process must not run setup. The application-owned pool must be configured for
autocommit because the setup migrations include `CREATE INDEX CONCURRENTLY`:

```python
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from langgraph.checkpoint.postgres import PostgresSaver

pool = ConnectionPool(
    database_url,
    kwargs={
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    },
    open=False,
)
pool.open(wait=True)
checkpointer = PostgresSaver(pool)
```

`PostgresSaver.from_conn_string()` is a context manager intended for scoped
usage. The long-lived application path must keep the pool alive for the graph
lifetime and close it during application shutdown. The migration command calls
`PostgresSaver(pool).setup()` before applying the SQL migrations, and
`011_agent_checkpoint_roles.sql` grants `omics_app` DML access to the three
runtime tables. No custom graph-state table or PlanStore is introduced: the
saver owns LangGraph checkpoint tables, while business thread, turn, and
message records remain in their existing stores.

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
- `.venv/Lib/site-packages/langgraph/checkpoint/postgres/__init__.py`:
  `PostgresSaver` constructor, `setup()`, and `delete_thread()`
- `.venv/Lib/site-packages/psycopg_pool/__init__.py`: `ConnectionPool` lifecycle

The signatures were inspected with `inspect.signature`, and the interrupt/resume
contract was additionally exercised with a local minimal graph.
