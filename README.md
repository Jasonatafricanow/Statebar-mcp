# statebar-mcp

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

## 产品介绍

### 解决什么痛点

**痛点 1：Agent 没有"短期状态记忆"，每轮都在裸奔**
长期 Memory 只适合存稳定事实（"用户是开发者"），不适合存临时状态（"刚睡醒 / 胃疼 / 下午可能去游泳"）。没有状态层时，Agent 每轮对话都要从零推断用户状态——推断错就翻车。经典案例：用户凌晨说"我刚睡醒"，Agent 还在催"这么晚该睡了"。

**痛点 2：状态跨会话、跨平台丢失**
用户在微信说"胃疼"，切到 Telegram 问天气——Agent 完全不知道胃疼这件事，聊天体验支离破碎。状态应该跟着**人**走，不是跟着**会话**走。

**痛点 3：上下文膨胀与污染**
把全部状态塞进 prompt 会膨胀且过时；更危险的是 Agent 自己猜的"事实"被写回记忆再注入自己，形成**自污染闭环**（AI 猜测 → 写入 → 注入 → 更确信猜测）。

**痛点 4：部署门槛杀死开源分发**
传统方案要用户自己常驻 daemon、配端口/token/systemd——开源项目一碰这个门槛就没人用。

### 满足什么需求

| 需求 | 实现 |
|---|---|
| Agent 每轮**确定性地**知道用户当前状态 | `snapshot` 每轮注入（固定底座 5-8 条 + query 增量 0-3 条，100-300 tokens） |
| 用户随口一句话就能更新状态 | `observe` 自然语言抽取（规则即时 + LLM 持久，双路径） |
| 状态有生命周期，不会永远挂着 | 语义窗口（下午=13:00-17:00）+ 惰性过期 + 显式取消/解决 |
| 区分"我打算去"和"我可能去" | certainty ∈ confirmed/planned/tentative/estimated/inferred |
| 跨微信/Telegram/飞书共享状态 | `subject_id` 作用域 + provenance 保留，不按平台隔离 |
| 不会重复处理同一条消息 | 两级幂等（event_id + observation_index） |
| 主动关心有素材 | `context_candidates`（recent / unresolved / interesting / planned） |
| 任何人能装、不用 daemon | MCP stdio：Client 拉起子进程、退出即消失、SQLite 持久化 |
| 免费档模型也能用 | 抽取器模型可配置；未配置 LLM 时规则路径照常工作 |

### 能完成什么功能

- **observe**：喂自然语言（"我刚睡醒"、"胃好多了"、"下午可能去写书法"、"不去了"）→ 自动抽取成结构化 Observation → 确定性 Reconciler 更新 Canonical State
- **snapshot**：每轮生成当前状态快照注入 Agent 上下文（自动淘汰过期状态、按 query 追加相关项）
- **context_candidates**：给主动性系统提供聊天素材（未闭环事件、最近事件、有趣事件）
- **get_state**：查询完整状态（调试 / 分析 / 校验）
- **确定性规则引擎**：R1-R10 业务规则 + S1-S2 系统规则（防自污染、保留历史）
- **多 transport**：MCP stdio（默认，零依赖）/ REST serve（多端共享）

### 典型使用场景

```
场景 1：凌晨刚醒
用户：我刚睡醒
Agent（当前轮）：难得睡到自然醒？睡够了没？   ← 不催睡（红线守住）

场景 2：跨平台状态
微信 14:00：胃有点疼
Telegram 16:00：好点了吗？                      ← 状态跟着人走

场景 3：计划管理
用户：下午可能去写书法
Agent（17:30 后）：不再问"还去写书法吗"        ← 语义过期
用户：不去了
Agent：好，那改天                        ← 显式取消

场景 4：主动关心素材
用户 14:00：胃疼（unresolved）
心潮达到主动阈值 → 读 context_candidates → "胃现在好点了吗？"
```

---

## 快速开始

> **发布状态**：PyPI 尚未发布。当前请从源码安装：

```bash
git clone https://github.com/christopher931649/Statebar-mcp.git
cd Statebar-mcp
pip install .            # 或开发模式：pip install -e ".[dev,mcp]"

# ① MCP stdio（默认分发，无 daemon，Client 拉起即用）
statebar-mcp mcp

# ② REST 常驻服务（多端共享：Adapter + 心潮 + Health + Diary；默认只绑本机回环）
statebar-mcp serve --host 127.0.0.1 --port 8765
```

> `pip install statebar-mcp` 在首个 PyPI 版本（0.1.0）发布后生效。

**零依赖**：core 全部 stdlib（sqlite3/json/threading/urllib）；MCP stdio 直接实现
MCP JSON-RPC 协议（已通过官方 `mcp` SDK 客户端互操作测试，测试见
`tests/test_mcp_stdio.py`）。

### ⚠️ 隐私与数据边界（使用前必读）

- **REST serve 默认只绑定 `127.0.0.1`，无认证。** 绑定非回环地址（如
  `--host 0.0.0.0`）前必须先设置认证令牌 `DSH_USER_STATE_SERVE_TOKEN`——
  服务会**拒绝**在无令牌时绑定非回环地址（fail-closed）。设置令牌后，
  **所有**端点（含 `/v1/health`）都要求 `Authorization: Bearer <token>`。
  请求体上限默认 64 KiB（`DSH_USER_STATE_MAX_BODY_BYTES`）。
- **启用 LLM 抽取 = 同意把用户的原始消息文本发给第三方模型端点。**
  发送内容仅限：抽取指令（系统提示词）+ 当条用户消息文本；**不会**发送
  subject_id、event_id、助手回复、历史状态或数据库内容。该路径默认**关闭**，
  必须显式设置 `DSH_USER_STATE_LLM_ENABLED=1` 且配置端点后才启用；
  推荐使用本地 Ollama（`http://127.0.0.1:11434/v1`）等不出境的端点。
  未启用时，规则路径（刚醒/睡了/取消/症状/吃药）照常工作，计划类抽取缺失。
- 数据全部落在本地 SQLite（`~/.statebar-mcp/user_state.db`），不包含任何遥测。

### MCP config 示例（Claude / Codex / 任意 MCP Agent）

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
statebar_mcp/
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
└── cli.py                   statebar-mcp mcp | serve | health
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

环境变量（也可写 `~/.statebar-mcp/config.json`）：

| 变量 | 说明 |
|---|---|
| `DSH_USER_STATE_DB` | SQLite 路径（默认 `~/.statebar-mcp/user_state.db`） |
| `DSH_USER_STATE_LLM_ENABLED` | **显式开关**：`1`/`true` 才允许把用户消息文本发送给 LLM 端点（隐私边界，默认关闭） |
| `DSH_USER_STATE_LLM_BASE_URL` | Persistent 抽取器 OpenAI 兼容端点（推荐本地 Ollama） |
| `DSH_USER_STATE_LLM_API_KEY` | API key |
| `DSH_USER_STATE_LLM_MODEL` | 模型名（部署配置项，架构不绑定模型） |
| `DSH_USER_STATE_LLM_MOCK=1` | 使用确定性 mock 抽取器（本地、无网络出口；离线开发/CI） |
| `DSH_USER_STATE_SERVE_HOST/PORT` | serve 绑定地址（默认 127.0.0.1:8765；非回环必须配 token） |
| `DSH_USER_STATE_SERVE_TOKEN` | Bearer 令牌；设置后所有端点（含 health）强制认证 |
| `DSH_USER_STATE_MAX_BODY_BYTES` | 请求体上限（默认 65536） |

未启用/未配置 LLM 时：同步 Fast Overlay 路径照常工作（刚醒/睡了/取消/症状/好多了/吃药），
Persistent 异步路径优雅禁用。

## 深度接入：确定性 Adapter（推荐给框架开发者）

普通 MCP 接入时，是否调用 `snapshot` 取决于 Host/模型自己（可能不调用）。
若需要**每轮 100% 确定性挂载**（如 Hermes），用深度 Adapter：

```
prefetch(current_user_message)
  1. observe(current_user_message)     ← 先喂（observe-before-snapshot）
  2. snapshot(query=current_user_message)
  3. 注入 Context
```

Hermes 侧参考实现见私有仓库 `hermes-user-state-adapter`（MCP client SDK
编程式调用，模型无选择权）。

## 开发与验收

```bash
pip install -e ".[dev,mcp]"
pytest          # 74 tests: 单元 + D1-D12 验收 + REST + MCP stdio（含官方 SDK 互操作）+ 安全/恢复
```

验收矩阵 `D1–D12` 与派单总纲 §15 一一对应（`tests/test_acceptance_d.py`）；
安全与恢复测试见 `tests/test_security.py`；CI 见 `.github/workflows/ci.yml`
（Ubuntu/Windows × Python 3.10-3.13）。

## License

MIT
