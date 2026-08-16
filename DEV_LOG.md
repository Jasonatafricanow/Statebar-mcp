# DEV_LOG — statebar-mcp

> 现场开发日志（个人机器上的部署细节不入公开仓库）。
> 派单来源：对话状态栏计划V1-派单总纲 v1.1（应用层语义冻结，唯一准绳）。

## 2026-08-16 · 派单 A 主体完成（D1-D12 全通过）

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
  - `service.py`：observe 同步路径 + 单 worker 异步路径（失败不影响同步 → D3）
- `transports/`：`contract.py`（五 API 冻结映射）+ `mcp_stdio.py`（零依赖 JSON-RPC，stdout 只走协议帧）+ `rest.py`（stdlib HTTP）
- `cli.py`：`statebar-mcp mcp | serve | health`
- 验收：**D1–D12 全部通过**（`tests/test_acceptance_d.py`）；MCP stdio 通过官方 `mcp` SDK ClientSession 互操作测试

### 上线验证（个人部署，细节不入公开仓库）
- 生产 serve 常驻（REST 回环 + 配置文件注入端点与模型）
- 真实 LLM 抽取实测：计划类语句 → tentative plan + 语义窗口 ✓
- 与私有 Hermes Adapter 联调：真实会话 observe 流量入库，awake 状态持久 ✓

### 关键设计决策
1. **零第三方依赖**：core/transports 全 stdlib；MCP stdio 直接实现 JSON-RPC（协议级兼容官方 SDK，已实测）
2. **时序守卫基准 = last_observed_at（语义时间）**而非 DB/进程时间：
   - D12 防止延迟消息回滚；
   - T10 同秒连发两条消息不被误判 stale（初版用内部时间误杀，已修复）
3. **provenance 由调用方拥有**：LLM 抽取器不能伪造 source（S1 可强制）
4. `_is_stale_creation` 必须查全状态（含终态）——终态 completed 正是不能回滚的对象（D12 修复点）

## 2026-08-16 · 安全与发布加固（评审修复）

按公开评审意见逐项修复（详见 commit 记录）：

1. **REST fail-closed + 认证**：默认回环；非回环无 token 拒绝启动；
   配 token 后所有端点（含 health）强制 Bearer 认证（constant-time）；请求体 64 KiB 上限（413）。
2. **LLM 抽取显式 opt-in**：新增 `DSH_USER_STATE_LLM_ENABLED`（默认关）；
   README 增加隐私与数据边界章节（发送内容=系统提示+当条用户消息；不发标识符/助手文本/历史/库内容）。
3. **异步线程生命周期**：改为单 worker + 队列（不再每条消息开线程）；
   `close()` 排空队列 → join worker → 才关闭 extractor/store。
4. **事件状态机可恢复**：`pending → sync_committed → complete/failed`；
   failed/pending 事件在重试 observe 时 resume（新 observation 才 reconcile，不重复写 transition）；
   sync_committed 且不在途时重新入队兜底进程崩溃丢队列。
5. **SECURITY.md**：真实支持版本（0.1.x）、上报渠道、威胁模型与信任边界。
6. **DEV_LOG 脱敏**：移除本机路径/PID/用户名/模型与网关细节（本文档）。
7. **安装说明可兑现**：README 改为源码安装，标注 PyPI 发布状态。
8. **CI**：`.github/workflows/ci.yml`（Ubuntu/Windows × Python 3.10-3.13）。
9. **env 布尔解析**：`"0"` 不再被当作 True（`_env_bool` 白名单）。
10. 新增 `tests/test_security.py`（认证/fail-closed/413/隐私开关/事件恢复/worker 生命周期）。

### 复评修复（第二轮，按评审复评逐项）

1. **[P1] MCP stdio 强制 UTF-8**：Windows 管道默认 ANSI 代码页（GBK），中文在协议边界被破坏。
   `force_utf8_stdio()` 显式 `reconfigure(encoding="utf-8")`，不依赖 PYTHONUTF8/PYTHONIOENCODING；
   回归测试在子进程环境剥离这两个变量后发送中文（此前全绿是继承 PYTHONUTF8 的假象）。
2. **[P1] observation 与 reconcile 原子性**：observations 表新增 `reconciled` 标记列；
   insert 与 reconcile 之间的崩溃窗口在下次 observe 时重放所有未 reconcile 的 observation，
   不再因 `INSERT OR IGNORE` 吞掉状态。顺带修复 v0.1 遗留 bug：observations 表原本没有
   `time_expression` 列（内存对象 reconcile 所以测试未暴露），补列 + 迁移，
   恢复路径从 DB 重读后语义窗口不再丢失。
3. **[P1] worker 关闭可靠 join**：close() 跳过未开始的排队任务（事件保持 sync_committed 可恢复），
   等待在途任务完成——宽限 = 抽取器自身超时 + 5s，仍存活则无界 join（受在途请求超时约束），
   确保 store 关闭前 worker 已退出。
4. **[P2] fail-closed 下沉**：非回环无 token 的拒绝从 CLI 下沉到 `create_server`，
   库调用方无法绕过。
5. **Windows RST(10053) 竞态**：401/413 拒绝路径先排空请求体再关闭连接，
   消除 keep-alive 未读数据触发 RST 导致的偶发 ConnectionAbortedError（8 连跑全绿）。
6. 测试从 74 → **78 passed**（新增：中文 stdio 无 PYTHONUTF8、崩溃窗口恢复、close 在途/丢弃语义、
   create_server fail-closed）；CI 增加 `workflow_dispatch` 与 tag 触发。

### 待办
- [ ] 首个 PyPI 发布（0.1.0，发布后 README 的 pip install 生效）
- [ ] 确认仓库 Actions 已启用（本地无法访问 GitHub API；工作流文件已在 master 树中，可手动 dispatch）
- [ ] mcp_http.py（Streamable HTTP，可选 transport，§5 标为可选；Phase 1 已交付 stdio+serve）
- [ ] 示例 adapter、开源社区收尾

## 派单总纲关键约束索引（实现自查）
- §6 五 API 语义冻结 → transports/contract.py
- §6.1 observe 输入（subject_id/event_id/source/text/observed_at）→ models.ObserveRequest
- §8 observe 同步/异步路径 → service.observe
- §10 R1-R10/S1-S2 → reconciler.py（每规则有独立 handler + 单元测试）
- §13 四表 → store.py
- §15 D1-D12 → tests/test_acceptance_d.py
