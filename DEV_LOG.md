# DEV_LOG — statebar-mcp

> 现场开发日志（详细复盘归知识库）。派单来源：`C:\知识库\项目\AI项目\对话状态栏计划V1-派单总纲v1.md`（v1.1，唯一准绳）。

## 2026-08-16 · 派单 A 主体完成（D1-D12 全通过）

### 上线记录（同日）
- 生产 serve 常驻：`C:\Python314\python.exe -m statebar_mcp serve --port 8765 --log-file ...`（PID 10520）
- 生产配置 `~/.statebar-mcp/config.json`：DB=`profiles/xiyue/state/user_state.db`；
  抽取器=Gemini OpenAI 兼容端点（gemini-3.1-flash-lite）
- 真实 LLM 抽取实测：'下午可能去写书法' → tentative plan + afternoon 窗口 ✓
- 派单 B 上线后：serve 收到溪月网关真实 observe 流量，awake 状态入生产库 ✓

### 完成内容
- 仓库骨架：`pyproject.toml`（零依赖 core；`[dev]`=pytest、`[mcp]`=官方 SDK 仅测试用）、`statebar_mcp/` 包、`tests/`
- `core/`：
  - `models.py`：Observation/Source/State/StateTransition/Snapshot 数据模型；`last_observed_at`（语义时间）字段
  - `store.py`：SQLite 四表（ingested_events/observations/states/state_transitions），两级幂等，WAL，线程安全
  - `extractor/fast_overlay.py`：12 条规则 + span-masking（睡醒了不触发睡眠规则；好多了+吃药同发两条）
  - `extractor/persistent.py`：OpenAI 兼容端点（stdlib urllib）+ MockExtractor；模型可配置（部署配置项）
  - `extractor/validator.py`：确定性校验（枚举/confidence 钳制/provenance 由调用方拥有/key 归一化）
  - `reconciler.py`：R1-R10 + S1/S2 + D12 乱序守卫（基于 last_observed_at 语义时间，同秒连发不误杀 → T10）
  - `lifecycle.py`：语义窗口（afternoon=13:00–17:00 本地语义）、TTL 兜底、R10 惰性过期
  - `snapshot.py`：BASE≤8 + query 增量≤3，最近终态（cancelled/completed/resolved）在 relevant_until 内短暂保留
  - `service.py`：observe 同步路径（幂等→Overlay→Reconciler→commit）+ 异步路径（后台线程，失败不影响同步 → D3）
- `transports/`：`contract.py`（五 API 冻结映射）+ `mcp_stdio.py`（零依赖 JSON-RPC，stdout 只走协议帧）+ `rest.py`（stdlib HTTP）
- `cli.py`：`statebar-mcp mcp | serve | health`
- 验收：**D1–D12 全部通过**（`tests/test_acceptance_d.py`，13 用例）；全套 62 passed
  - D1-D4 走真实 REST 传输；D5-D12 服务层 + 可控时钟 + mock 抽取器
  - MCP stdio 通过官方 `mcp` SDK 2.0 ClientSession 互操作测试

### 关键设计决策
1. **零第三方依赖**：core/transports 全 stdlib；MCP stdio 直接实现 JSON-RPC（协议级兼容官方 SDK，已实测）
2. **时序守卫基准 = last_observed_at（语义时间）**而非 DB/进程时间：
   - D12 防止延迟消息回滚；
   - T10 同秒连发两条消息不被误判 stale（第一次实现用 updated_at 微秒时间 → 误杀，已修复）
3. **provenance 由调用方拥有**：LLM 抽取器不能伪造 source（S1 可强制）
4. `_is_stale_creation` 必须查全状态（含终态）——终态 completed 正是不能回滚的对象（D12 修复点）
5. PowerShell 5.1 `Set-Content -Encoding UTF8` 会加 BOM/损坏中文 → 一律用 write/edit 工具改文件

### 环境备注
- 本机 `PYTHONPATH` 全局指向 Hermes runtime site-packages（3.13 wheel），污染 Python 3.14：
  运行测试/命令前必须 `$env:PYTHONPATH=""`；pytest 需禁用 langsmith 插件（已在 pyproject addopts 固化）
- `C:\Python314` 已装 `mcp` SDK（供互操作测试）；包本体可编辑安装到 user site-packages

### 待办
- [ ] mcp_http.py（Streamable HTTP，可选 transport，§5 标为可选；Phase 1 已交付 stdio+serve）
- [ ] 派单 B：Hermes MemoryProvider Adapter（私有）→ T1-T10
- [ ] 开源前：示例 adapter、CI、通用化收尾

## 派单总纲关键约束索引（实现自查）
- §6 五 API 语义冻结 → transports/contract.py
- §6.1 observe 输入（subject_id/event_id/source/text/observed_at）→ models.ObserveRequest
- §8 observe 同步/异步路径 → service.observe
- §10 R1-R10/S1-S2 → reconciler.py（每规则有独立 handler + 单元测试）
- §13 四表 → store.py
- §15 D1-D12 → tests/test_acceptance_d.py
