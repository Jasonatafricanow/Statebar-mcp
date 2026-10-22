# Statebar MCP

[English](README.md) | [简体中文](README.zh-CN.md)

A small MCP and REST service that turns user observations into a current-state snapshot: waking up, changed plans, symptoms and ongoing activities.

**Language support:** the built-in deterministic extraction rules currently recognize Chinese. English text is not covered by an equivalent built-in rule pack. Optional model extraction is a separate, explicitly configured path.

## Why reconciliation matters

Discussing sleep does not mean the user is asleep. Assistant messages cannot establish user state; older observations cannot roll back newer state; a new interaction can invalidate a sleeping state.

For example, `我刚睡醒` (“I just woke up”) is a Chinese rule example. The English translation describes its meaning, not an English rule that is currently implemented.

## Run and verify

```bash
python -m pip install -e ".[dev,mcp]"
statebar-mcp mcp
# Alternative transport:
statebar-mcp serve --host 127.0.0.1 --port 8765
# Tests:
python -m pytest -q
```

Source installation is the documented distribution path. See [client configuration, state semantics and privacy](docs/IMPLEMENTATION.md).

## Interface and boundaries

Five MCP tools expose observation, snapshots, context candidates, state and health. SQLite stores state locally. If optional LLM extraction is enabled, user text goes to that configured endpoint. MCP initialization advertises the implemented `2024-11-05` protocol rather than echoing arbitrary client versions. English rules and package publication remain follow-ups.

## License

MIT — see [LICENSE](LICENSE).
