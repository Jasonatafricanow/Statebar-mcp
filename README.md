# Statebar MCP

**A current-state layer for AI agents.**

Statebar sits between long-term memory and the active conversation. It maintains short-lived user state — what is true now, what recently changed, what remains unresolved, and what is likely to expire — without turning every model inference into canonical fact.

In the wider portfolio, Statebar is the smallest concrete expression of a recurring design question:

> How should an agent carry useful state forward without letting extraction, retrieval, or its own previous language become authority?

## Portfolio role

```text
long-term memory
      |
      v
Statebar MCP      <- current / short-lived user state
      |
      v
active agent context
```

Statebar is deliberately narrower than [Mind Runtime](https://github.com/Jasonatafricanow/Mind-Runtime). It solves a bounded state-maintenance problem and exposes it through MCP stdio or REST.

## The problem

Long-term memory is good at stable facts and historical context. It is a poor place for states such as:

- just woke up;
- stomach discomfort is improving;
- a plan is tentative;
- an event was cancelled;
- an unresolved issue is still relevant this afternoon.

Putting all of this in chat history creates repeated inference and stale context. Writing every model interpretation back to memory is worse: the model can manufacture a fact, read it again later, and become more confident in its own mistake.

The system therefore needs a distinction between:

```text
signal / language
  -> observation
  -> deterministic admission and reconciliation
  -> canonical current state
  -> bounded snapshot for the agent
```

## How the design evolved

### 1. Rule-first extraction

The first version used a fast rule overlay plus an optional LLM extraction path. This made common updates cheap and deterministic while allowing richer language to be structured when an LLM was explicitly enabled.

The weakness was that extraction quality and state authority were too easy to conflate.

### 2. False positives exposed the real boundary

A concrete failure occurred when discussion *about* a state could be interpreted as the state itself. Sleep-related words inside meta-discussion were enough to produce a sleep observation.

That failure changed the architecture rather than only adding another regex exception.

The current direction treats extraction as candidate production. Semantic admission is checked against the authoritative request text before a candidate can affect canonical sleep/wake state. The same principle generalizes beyond sleep:

> Recognizing a phrase is not the same as establishing a state.

### 3. From extraction pipeline to evidence-driven state engine

V2 introduces interaction itself as first-class evidence. If a user is actively sending a message while canonical state says `sleeping`, the interaction can be evidence of wakefulness — subject to temporal ordering and evidence-priority rules.

The important shift is:

```text
"understand this sentence"
        |
        v
"maintain the best current state from multiple evidence sources"
```

## Key design decisions

### Observation is not state

LLMs and rules produce observations. They do not directly CRUD canonical state. A deterministic reconciler owns state transitions.

### Assistant output cannot establish user state

The agent's own language is never evidence that the user entered a state. This prevents a self-pollution loop.

### Semantic time matters

`observed_at` is treated as the time of the evidence, not merely database insertion time. Delayed messages must not roll newer state backward.

### State follows the subject, not the chat surface

Canonical state is scoped by `subject_id`. Provenance records the source platform, but switching clients does not silently create a different person.

### Lifecycle is semantic before it is numeric

Plans and temporary states can expire by semantic windows such as "this afternoon". TTL exists as a fallback, not as the primary meaning of time.

### Transport is not domain logic

MCP stdio and REST expose the same application semantics. The core state engine is transport-neutral.

## Architecture

```text
Conversation / Device / Diary / System
                |
                v
              Signal
                |
                v
           Observation
                |
                v
     Evidence / Ontology Policy
                |
                v
        Transition Intent
                |
                v
     Deterministic Reconciler
                |
                v
         Canonical State
          /           \
         v             v
    Snapshot     Context Candidates
         |             |
         +------> Agent / Host
```

## Current implementation

Statebar currently provides:

- `observe` — ingest text or interaction evidence;
- `snapshot` — produce a bounded current-state context;
- `context_candidates` — expose recent / unresolved / planned items;
- `get_state` — inspect canonical state;
- semantic expiration and explicit cancellation / resolution;
- event- and observation-level idempotency;
- provenance and transition history;
- MCP stdio and REST transports;
- optional LLM extraction with an explicit privacy opt-in;
- SQLite persistence with no required external service.

## Verification

The repository contains regression coverage for V1 and V2 state semantics, including evidence priority, delayed observations, assistant-message exclusion, explicit sleep/wake language, and interaction-derived wakefulness.

Run:

```bash
pip install -e ".[dev,mcp]"
pytest -q
```

## Boundaries and non-claims

Statebar is not:

- a general long-term memory system;
- a health platform;
- a full planner or task manager;
- an autonomous agent;
- a claim that language extraction is perfectly reliable.

MCP-only integration also cannot force a host to request a snapshot every turn. Hosts that require deterministic attachment should integrate at the adapter layer.

## Privacy model

The default core path is local and SQLite-backed. Optional LLM extraction is disabled unless explicitly configured. When enabled, raw user message text is sent to the configured endpoint; operators should choose that endpoint according to their privacy requirements.

REST defaults to loopback. Non-loopback binding requires an authentication token.

## Quick start

```bash
git clone https://github.com/Jasonatafricanow/Statebar-mcp.git
cd Statebar-mcp
pip install -e ".[dev,mcp]"

statebar-mcp mcp
# or
statebar-mcp serve --host 127.0.0.1 --port 8765
```

MCP configuration:

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

## Engineering philosophy

The project intentionally keeps probabilistic interpretation outside the authority boundary:

> Models may propose meaning. Deterministic runtime rules decide what becomes state.

That principle later appears in broader form across Mind Runtime and LCE: derived structure can be useful without automatically becoming truth.
