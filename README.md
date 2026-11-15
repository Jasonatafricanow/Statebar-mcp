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

## Host-extracted observations

### Why

In agent deployments Statebar is not used on its own: it sits behind a host agent that already reads every user message. Two problems showed up in practice.

1. **The agent skipped the state layer.** The host rarely called state tools by itself. Instead it wrote "facts" about the user straight into its long-term memory from conversation text, and when it reconstructed details later it moved events to the wrong day or turned guesses into precise numbers (a stated "around 4 a.m." became "fell asleep at 4:20"). The validation that would have caught this was bypassed, not broken. A wrong memory is worse than a missing one, because later reasoning builds on it.
2. **The obvious fix costs a second inference.** Enabling Statebar's own LLM extractor means interpreting the same message twice: once by the host to reply, and again by a dedicated extraction call. That is extra cost and latency for understanding the host already has, so in that deployment the extractor stayed off and only fast rules ran. Plans, symptoms and activities were never captured.

### What changed

`observe` accepts an optional `observations` array in the persistent extractor's JSON shape. The host proposes observations as a by-product of the turn it is already running. Statebar keeps the authority:

- every item still passes the deterministic validator against `text` (the user's original words), including sleep/wake semantic admission. A mention, a question or a reconstruction is rejected just as it would be from the built-in extractor;
- the observations are stored as their own idempotent event `<event_id>#host` and reconciled at the message's original semantic time with the strict replay rules used for late analyses, so they cannot revive or downgrade newer state;
- they add no interaction evidence;
- without the field, `observe` behaves exactly as before.

```json
{"subject_id": "user-001", "event_id": "tg:msg-123", "text": "下午可能去写书法", "observed_at": "…",
 "observations": [{"type": "plan", "category": "planning", "key": "calligraphy", "value": "go practice calligraphy",
                   "certainty": "tentative", "time_expression": "afternoon", "confidence": 0.9,
                   "raw_payload": "下午可能去写书法"}]}
```

Intended host flow:

```text
observe(event_id, user text)              before the reply: fast rules + interaction evidence
host replies, emits observation JSON       same inference, no extra model call
observe(event_id, user text, observations) after the reply: validated + reconciled as <event_id>#host
accepted state changes -> downstream       memory / logs receive authenticated state,
                                           not the agent's own recollection
```

This makes Statebar the gate in front of downstream tools: a current-state fact reaches them only after it has passed evidence rules.

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

- `observe` — ingest user text or interaction evidence, optionally with host-extracted observations;
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

Optional LLM extraction is disabled unless configured. If enabled, raw user text is sent to the configured endpoint. Host-extracted observations need no extra endpoint: they come from the model the host already uses.

REST defaults to loopback. Non-loopback binding requires an authentication token.

## Bounded completeness

Statebar stays small because it owns only short-lived current-state reconciliation.

Its complete contract is narrower than a memory or agent runtime:

```text
interaction evidence
-> observation candidate
-> semantic/source admission
-> deterministic reconciliation
-> current state + transition history
-> bounded snapshot
```

Long-term memory, planning, general cognition, tool execution and agent orchestration are
explicitly outside the service. Adding those capabilities would not make this state layer
more complete; it would give it authority that belongs elsewhere.

The useful evidence is therefore the transition semantics—stale-event handling, provenance,
assistant-message exclusion, replay/idempotency, lifecycle rules and transport behavior—not
the amount of code wrapped around them.

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
- host-extracted observations (admission against the user's words, idempotency, late-analysis ordering);
- MCP and REST behavior.

## Development history

The original development log is retained under
[`docs/history/DEV_LOG.md`](docs/history/DEV_LOG.md) as dated historical evidence. It is
not current architecture authority; several V1/V2 names in that log predate the current
simplification.

## Scope

Statebar is intentionally narrow. It is not a long-term memory database, planner, health application, or full agent runtime.

Its job is to keep a small amount of current state consistent enough for another agent/host to consume.