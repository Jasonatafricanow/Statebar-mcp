# Statebar MCP

[English](README.md) | [简体中文](README.zh-CN.md)

A small MCP and REST service that turns user observations into a current-state snapshot: waking up, changed plans, symptoms and ongoing activities.

**Language support:** built-in Chinese rules plus a small English whole-message rule pack for waking, preparing for sleep, headache onset/resolution and taking medication. This is not equivalent coverage of arbitrary English conversation. Optional model extraction is a separate, explicitly configured path.

## Why reconciliation matters

Discussing sleep does not mean the user is asleep. Assistant messages cannot establish user state; older observations cannot roll back newer state; a new interaction can invalidate a sleeping state.

For example, both `我刚睡醒` and `I just woke up.` trigger an awake observation. English rules require a complete first-person statement; quoted, negated, conditional, historical and third-person examples are not admitted by this rule pack.

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

Five MCP tools expose observation, snapshots, context candidates, state and health. SQLite stores state locally. If optional LLM extraction is enabled, user text goes to that configured endpoint. MCP initialization advertises the implemented `2024-11-05` protocol rather than echoing arbitrary client versions. Broader English coverage and package publication remain follow-ups.

## License

MIT — see [LICENSE](LICENSE).
