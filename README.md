# Statebar MCP

Statebar is a small Python service for short-lived user state.

It accepts observations from conversation or interaction events, reconciles them against existing state, stores the result in SQLite, and exposes the current snapshot through MCP stdio or REST.

## Data flow

```text
user message / interaction event
        |
        v
extract observations
        |
        v
validate source + semantic time
        |
        v
propose state changes
        |
        v
deterministic reconciler
        |
        v
SQLite state + transition history
        |
        v
snapshot / context candidates
```

Model/rule extraction does not write state directly.

## Why the extra reconciliation step exists

An earlier version matched sleep-related text too aggressively. Discussion *about* sleep could produce a `sleeping` observation even when the user never said they were going to sleep.

The fix was not only another regex exception. Sleep/wake candidates are now checked against the original user message and source type before they may update stored state.

This regression is covered by tests.

## State handling

The current implementation tracks:

- current/temporary state;
- plans and cancellations;
- symptom/activity updates;
- sleep/awake state;
- semantic validity/relevance windows;
- transition history;
- observation provenance;
- replay/idempotency markers.

Older observations do not roll newer state backward.

Assistant-authored text is not accepted as evidence that the user entered a state.

## Evidence from interaction

Real interaction can itself be evidence.

For example, a current user-authored interaction can invalidate an older `sleeping` state without inventing a precise wake-up time.

The rule is implemented in normal Python state-transition logic, not by asking an LLM to decide final state.

## Main modules

```text
statebar_mcp/core/
  extractor/        rule/model extraction + validation
  models.py         observations, state, transitions
  inference.py      proposes state changes
  reconciler.py     applies validated changes
  lifecycle.py      time/relevance windows
  ontology.py       state relations/rules
  snapshot.py       bounded current-state output
  store.py          SQLite persistence

statebar_mcp/transports/
  mcp_stdio.py
  rest.py
```

Some module names predate the current simplification work; for example `ontology.py` is mostly a collection of state relations and lifecycle rules rather than a general ontology system.

## Interfaces

Core operations include:

- `observe` — ingest user text or interaction evidence;
- `snapshot` — return bounded current state;
- `context_candidates` — return recent/relevant items;
- `get_state` — inspect stored state.

Run as MCP:

```bash
pip install -e ".[dev,mcp]"
statebar-mcp mcp
```

Run as REST:

```bash
statebar-mcp serve --host 127.0.0.1 --port 8765
```

Example MCP configuration:

```json
{
  "mcpServers": {
    "user-state": {
      "command": "statebar-mcp",
      "args": ["mcp"]
    }
  }
}
```

## Privacy / deployment

The default store is local SQLite.

Optional LLM extraction is disabled unless configured. If enabled, raw user text is sent to the configured endpoint.

REST defaults to loopback. Non-loopback binding requires an authentication token.

## Verification

```bash
pip install -e ".[dev,mcp]"
pytest -q
```

Regression coverage includes:

- evidence priority;
- delayed observations;
- assistant-message exclusion;
- explicit sleep/wake statements;
- interaction-derived wakefulness;
- replay/idempotency;
- MCP and REST behavior.

## Scope

Statebar is intentionally narrow. It is not a long-term memory database, planner, health application, or full agent runtime.

Its job is to keep a small amount of current state consistent enough for another agent/host to consume.