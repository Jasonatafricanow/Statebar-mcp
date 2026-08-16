# dsh-user-state

**Hermes User State Layer** —— AI Agent 的外置短期工作记忆：轻量、持续更新、跨平台共享的**用户状态层**。

它位于长期 Memory 与当前 Conversation 之间，让 Agent 天然知道用户「现在怎么样、最近发生了什么、准备做什么、哪些事情未闭环」。

```
长期 Memory（稳定身份/偏好/习惯）
      │
User State Layer（刚睡醒 / 胃疼 / 今天游泳了 / 下午可能写书法）
      │
当前 Conversation
```

> 本项目不是「个人健康数据平台」。Health 只是 State 的数据源之一。
> 设计准绳：`对话状态栏计划V1-派单总纲v1.md`（应用层语义已冻结）。

---

## 快速开始

```bash
pip install dsh-user-state

# ① MCP stdio（默认分发，无 daemon，Client 拉起即用）
dsh-user-state mcp

# ② REST 常驻服务（多端共享：Hermes Adapter + 心潮 + Health + Diary）
dsh-user-state serve --host 127.0.0.1 --port 8765
```

**零依赖**：core 全部 stdlib（sqlite3/json/threading/urllib）；MCP stdio 直接实现
MCP JSON-RPC 协议（已通过官方 `mcp` SDK 客户端互操作测试，测试见
`tests/test_mcp_stdio.py`）。

### MCP config 示例（Claude / Codex / 任意 MCP Agent）

```json
{
  "mcpServers": {
    "user-state": {
      "command": "dsh-user-state",
      "args": ["mcp"]
    }
  }
}
```

### REST 端点（应用层 Contract 冻结，所有 transport 同一套语义）

| 应用层 API | MCP Tool | REST |
|---|---|---|
| observe | `user_state.observe` | `POST /v1/observe` |
| get_snapshot | `user_state.snapshot` | `GET /v1/snapshot?subject_id=&query=` |
| get_context_candidates | `user_state.context_candidates` | `GET /v1/context-candidates?subject_id=` |
| get_state | `user_state.get_state` | `GET /v1/state?subject_id=&category=&key=` |
| healthcheck | `user_state.health` | `GET /v1/health` |

observe 输入（语义冻结）：

```json
{
  "subject_id": "user-001",
  "event_id": "weixin:msg-123",
  "source": {"type": "conversation", "platform": "weixin", "session_id": "", "message_id": ""},
  "text": "我刚睡醒",
  "observed_at": "ISO8601（语义时间，由调用方传，非服务端入库时间）"
}
```

snapshot 输出示例（每轮刷新、自动淘汰无效、100–300 tokens、固定底座 + query 增量）：

```
CURRENT USER STATE
- awake since ~04:00
- stomach discomfort: improving
- medication taken recently
- swam today
- afternoon calligraphy plan: cancelled
```

## 架构

```
dsh_user_state/
├── core/                    ← Transport-neutral，应用层 Contract 冻结
│   ├── extractor/           Fast Overlay（规则，同步）+ Persistent（LLM Structured，异步）
│   ├── reconciler.py        R1-R10 + S1-S2（冻结规则集）
│   ├── lifecycle.py         semantic window（语义窗口优先于 TTL）
│   ├── snapshot.py          Current Snapshot（BASE + query 增量）
│   ├── service.py           observe/get_snapshot/get_state/get_context_candidates
│   └── store.py             SQLite 四表
├── transports/
│   ├── mcp_stdio.py         默认分发（零依赖 JSON-RPC）
│   └── rest.py              serve 模式
└── cli.py                   dsh-user-state mcp | serve | health
```

### 冻结语义速览

- **状态作用域 = subject_id**（跨平台共享；provenance 保留但不按 platform 隔离）
- **两级幂等**：event-level `UNIQUE(subject_id, event_id)`；observation-level
  `UNIQUE(subject_id, event_id, observation_index)`（一条 event 可产生多条 Observation）
- **Plan ≠ Idea**：certainty ∈ confirmed/planned/tentative/estimated/inferred
- **生命周期**：created → tentative/planned/active → completed/cancelled/resolved/superseded/expired
- **语义窗口**（不是 TTL）：`afternoon = 13:00–17:00`（本地语义）；TTL 仅兜底；
  过期在**读取时惰性执行**（R10），不依赖 cron
- **生命周期 ≠ 对话相关性**：分别保存 `valid_until` / `relevant_until`
- **防自污染（S1）**：Assistant 自己的话永远不能建立 Canonical State
- **冲突按 semantic observed_at 排序（D12）**：延迟消息不得回滚状态
- **S2**：所有更新保留 transition/history，不做破坏性覆盖
- **LLM 不直接 CRUD State**：LLM → Observation JSON → deterministic Validator → Reconciler

## 配置

环境变量（也可写 `~/.dsh-user-state/config.json`）：

| 变量 | 说明 |
|---|---|
| `DSH_USER_STATE_DB` | SQLite 路径（默认 `~/.dsh-user-state/user_state.db`） |
| `DSH_USER_STATE_LLM_BASE_URL` | Persistent 抽取器 OpenAI 兼容端点 |
| `DSH_USER_STATE_LLM_API_KEY` | API key |
| `DSH_USER_STATE_LLM_MODEL` | 模型名（部署配置项，架构不绑定模型） |
| `DSH_USER_STATE_LLM_MOCK=1` | 使用确定性 mock 抽取器（离线开发/CI） |
| `DSH_USER_STATE_SERVE_HOST/PORT` | serve 绑定地址（默认 127.0.0.1:8765） |

未配置 LLM 时：同步 Fast Overlay 路径照常工作（刚醒/睡了/取消/症状/好多了/吃药），
Persistent 异步路径优雅禁用。

## 开发与验收

```bash
pip install -e ".[dev,mcp]"
pytest          # 62 tests: 单元 + D1-D12 验收 + REST + MCP stdio（含官方 SDK 互操作）
```

验收矩阵 `D1–D12` 与派单总纲 §15 一一对应（`tests/test_acceptance_d.py`）。

## License

MIT
