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

### 复评第三轮（P1 原子性 + P2 有界排空）

1. **[P1] reconcile 与 reconciled 标记同一事务**：store 增加可嵌套 `transaction()`
   （BEGIN IMMEDIATE + 深度计数，所有写方法在事务内推迟 commit）；observe 同步路径
   与异步 worker 把「observation 插入 + state/transition 写入 + reconciled 标记 +
   事件状态」放进**单次 commit**——崩溃要么全有要么全无，重放不可能双写。
2. **[P1] observation 级幂等键**（双保险，兼治旧版本遗留的崩溃窗口）：
   states 表新增 `last_observation_key`（`<event_id>:<observation_index>`），
   `_mutate/_supersede` 对同一 observation 的重放直接 no-op；
   `_is_stale_creation` 同样拦截，覆盖创建路径。评审复现（1→2 条 transition）
   已固化为 `test_replay_after_reconcile_before_mark_is_idempotent`。
3. **[P2] 拒绝路径有界排空**：`_drain_request_body` 每次读取受 `drain_timeout`(0.5s)
   套接字超时约束；所有连接在 `setup()` 统一设 `request_timeout`(10s)。
   新增 `test_incomplete_body_401_is_bounded`（声明 Content-Length 不发正文，
   401 必须在 3s 内返回）。
4. 测试 78 → **80 passed**（6 连跑全绿）；test_unit 的 obs() 助手改为唯一
   event_id（幂等键在现实中以 UNIQUE(event_id,index) 为前提）。

### 复评第四轮（P1 历史 observation 去重边界）

1. **持久化已应用记录**：新增 `observation_applications` 表（tombstone，append-only，
   PK=(subject_id,event_id,observation_index)）。`reconciler.apply()` 入口先查 tombstone、
   规则成功后同事务写入——不受 state.last_observation_key 被后续 observation 覆盖的影响。
   迁移时从 reconciled=1 的 observations 与 states.last_observation_key 回填。
2. **重放严格冲突守卫**（治旧数据）：observation 增加瞬时 `is_replay` 标记，仅对
   恢复重放的 observation 生效——目标状态若已被**不同** observation 在同一或更晚
   语义时间占有，则拒绝变更/创建/复活（评审三步复现：legacy A=awake 未标记，
   B=sleep 同时刻接管，重放 A 不再复活 awake / 推翻 sleeping）。
3. 评审复现固化为 `test_legacy_replay_same_timestamp_no_resurrection`；
   补 b5606ef（source 字符串）回归测试 `test_source_string_form_accepted`。
4. 测试 80 → **82 passed**（6 连跑全绿）。
5. 推送 `v0.1.0-rc1` 标签触发 CI（workflow 对 `v*` 标签触发）。

## 2026-08-16 · V2 第一阶段：证据驱动状态引擎（垂直切片）

按《Statebar V2 证据驱动状态引擎重构 Spec》§20 实施，**不重写 V1，只加一条完整垂直切片**：

- `core/ontology.py`（新）：INCOMPATIBLE 本体关系（phase 1: awake↔sleeping）、
  证据规则（interactive_activity → 强 awake 证据）、声明式生命周期
- `core/inference.py`（新）：InferenceEngine（无副作用纯函数）——
  Observation + State + Ontology → TransitionIntent[]；
  **证据优先级**：显式语言 ≥ 行为推断（同秒"我睡了"不被 interaction 唤醒）
- `core/models.py`：Certainty.observed、SourceType.interaction/system、
  TransitionIntent（ESTABLISH/UPDATE/SUPERSEDE/RESOLVE/EXPIRE/NOOP，非 LLM 输出契约）
- `core/service.py`：observe 同步路径自动生成 `interactive_activity` Observation
  （conversation 源、非 assistant；diary 不算实时交互）
- `core/reconciler.py`：apply() 先走 Inference→Intent，未接管的 observation 回退
  V1 规则（R1-R10 保留，V2 §14 迁移策略）；Intent 执行带全部安全门
  （stale/replay/幂等/transition 历史/事务）；UPDATE 无实质变化时只 touch 确认时间
- `core/snapshot.py`：awake_confirmed → "awake confirmed at ~HH:MM"（不虚构 wake time）；
  显式"我刚睡醒"升级为 "awake since ~HH:MM"
- `tests/test_v2.py`：**V2-T1..T8 全部通过** + 证据优先级测试；全套 **91 passed ×4 连跑**
  （V1 的 D1-D12/安全/MCP stdio 全部保持绿 = V2-T8 外部 Contract 兼容的证据）

### 待办
- [ ] 首个 PyPI 发布（0.1.0，发布后 README 的 pip install 生效）
- [ ] 确认仓库 Actions 已启用（本地无法访问 GitHub API；工作流文件已在 master 树中，可手动 dispatch）
- [ ] mcp_http.py（Streamable HTTP，可选 transport，§5 标为可选；Phase 1 已交付 stdio+serve）
- [ ] 示例 adapter、开源社区收尾

## 2026-08-17 · 复评第五轮：P1/P2 修复 + P3 补测试

复评结论（11ce80b）：V2 基础行为成立、核心 91 项通过，但仍有 2 个 P1、3 个 P2。
本轮逐项修复并补测试（全套 108 passed，测试基数为 91 + 新增 17）：

1. **[P1] reconcile() 公开入口半提交**：`service.reconcile()`（transports 直接携带
   Observation 的入口）改为 `with store.transaction():` 包裹 —— state、transition、
   tombstone 同一事务原子提交。复评故障注入结果（首败后 state=1/transition=0、
   重试只补 tombstone、transition 永久丢失）固化为
   `TestReconcileTransactionAtomicity`：transition 写入失败 → 全部回滚（无半提交
   state、无 tombstone），重试完整恢复（1 state + 1 transition + tombstone）。
2. **[P1] V2 Inference 绕过 S1**：`is_interaction_observation()` 增加受信来源校验
   （`source.type == interaction`）；同时把 S1 门从 V1 handler 前移到
   `reconciler.apply()` 入口 —— 无论 V1 规则还是 V2 inference 接管，"助手内容
   永不创建用户状态" 现在是全入口边界。复评构造（assistant_question 来源的
   interactive_activity/observed）固化为回归测试，同形 conversation 来源同样被拒。
3. **[P2] transition legality gate 未接线**：`ontology.validate_transition()` 接入
   `_execute_intents` —— SUPERSEDE / 状态变更 UPDATE / RESOLVE / EXPIRE 写入前统一
   校验，非法跳转（如复评注入的 sleep ACTIVE→RESOLVED）整条 intent 拒绝、状态与
   历史均不动；PLAN_LIFECYCLE 补 SUPERSEDED（reschedule 语义）。附拒绝 + 合法
   生命周期放行测试。
4. **[P2] intent evidence 未持久化**：`TransitionIntent.evidence` 现在被消费 ——
   `_transition` 把 evidence 键（`<event_id>:<index>`）解析为 observations 表真实
   行 ID 写入 `source_observation_id`（V1 路径也一并获得血缘）。T3 升级为血缘断言：
   supersede transition 的 `source_observation_id` 必须等于 evidence observation 行 ID。
5. **[P2] Adapter 验收时钟失配**（hermes-user-state-adapter 仓库）：Provider 增加
   可注入时钟，测试统一冻结 observed_at（详见该仓库 DEV_LOG）。
6. **[P3] 补测试**：反向场景（awake→sleeping supersede 仍由 V1 R1/R2 管，reason
   含 R2 证明 V1 所有权）与混合场景（sleeping + interaction 同秒，显式语言 ≥ 行为
   推断，不建 awake）。

## 2026-08-17 · V2 第二阶段迁移：R3-R5/R6-R8 → ontology 驱动 inference

按《Statebar V2 证据驱动状态引擎重构 Spec》§22 迁移方法逐个落地
（ontology 声明 → inference handler → 删 V1 规则 → 全套测试绿）：

- `models.TransitionIntent` 扩展语义窗口字段（valid_from/valid_until/relevant_until、
  followup_relevant 三态）——ESTABLISH/UPDATE 由 inference 显式携带窗口，
  reconciler 只负责校验与执行
- **plan 切片（PLAN_LIFECYCLE）**：`plan/cancel` observation 由 inference 接管；
  activity 观察的部分所有权（有活跃 plan 目标 → completed intent，否则回退 V1 R4
  活动态）；R6 reschedule 改为 [ESTABLISH 新窗口, SUPERSEDE 旧 episode] 顺序，
  规避幂等守卫误杀同 observation 的新 episode；R9 显式确认升级同键推断态并入
  inference。删除 V1 的 `_r3_cancel/_r5_confirm/_r6_reschedule/_r_plan_new/
  _create_plan_state` 与 R4 的 plan 分支
- **symptom 切片（SYMPTOM_LIFECYCLE）**：`symptom/resolve` observation 由
  inference 接管 —— ESTABLISH(active, followup) / UPDATE(improving/resolved,
  re-affirm) / 新 episode。删除 V1 的 `_r_symptom_new/_r7_improving/_r8_resolved/
  _resolve_handler`
- 新增语义测试不需要加规则代码：迁移验收测试直接断言 inference 所有权、生命周期
  链、V1 处理器已删除（`_RULE_HANDLERS` 无 plan/cancel/symptom/resolve）

### 迁移后 V1 规则现状（下一轮继续）
- 剩余 V1：R1/R2（显式睡眠/清醒语言，interaction 切片只管行为推断）、R4 活动态
  （activity states）、R9（跨类型显式 supersede 推断）、R10（lazy_expire 保留）、
  S1/S2 系统规则
- 全套 **109 项测试 = 91 基线 + 18 新增**（复评回归 5：TrustBoundary 3 +
  LegalityGate 2；P3 2；迁移验收 9：plans 5 + symptoms 4；事务原子性 2）；
  本机 108 passed，另 1 项 MCP SDK 子进程互操作测试受本机沙箱命名管道限制
  无法运行（常规环境通过）

## 派单总纲关键约束索引（实现自查）
- §6 五 API 语义冻结 → transports/contract.py
- §6.1 observe 输入（subject_id/event_id/source/text/observed_at）→ models.ObserveRequest
- §8 observe 同步/异步路径 → service.observe
- §10 R1-R10/S1-S2 → reconciler.py（每规则有独立 handler + 单元测试）
- §13 四表 → store.py
- §15 D1-D12 → tests/test_acceptance_d.py

## 2026-08-20 · R10 收尾：ACTIVE/IMPROVING 惰性过期语义定义

按 2026-08-16 review P2 记录（sleeping 永不被 TTL 过期）与 V2 第二阶段迁移清单 R10 行
（"ACTIVE 状态的 TTL 语义要定义清楚"）落地：

- `lifecycle.lazy_expire` 从「只处理 TENTATIVE/PLANNED/PENDING」改为「除 EXPIRED 外
  所有带 valid_until 的状态，窗口过后一律惰性过期」——hunger/headache/hangover 等
  症状不再永久 active（SYMPTOM_LIFECYCLE 由 observation 驱动解决，TTL 为次级兜底）；
  sleeping/awake 的 TTL（12h/16h）同样生效，interaction 仍是 sleeping 的主消除器
  （V2 切片行为不变）。
- 新增回归测试 `test_lazy_expire_expires_active_and_improving_after_window`
  （旧代码下会失败：ACTIVE/IMPROVING 永不过期）。
- 删除 `lifecycle.py.bak-20260818`（变更已入 git）。
- 全量测试：本机 97 passed（本环境沙箱阻断 12 项 tmp_path/子进程用例，非代码回归；
  常规环境全量 109 项通过）。

## 2026-08-20 · 复评第六轮：D8 异步重抽取竞态修复 + R9 证据优先级统一迁移

### 1. [fix] D8 验收竞态（async worker 重抽取降级/复活状态）

复现：D8 约 1/3 随机失败（症状在 resolve 后仍显示 active）。根因是异步 worker 对
已同步事件的持久化重抽取携带事件原始语义时间、以同 event_id 迟到到达：

- 症状被 e8 置 IMPROVING 后，e7 的重抽取（e7:2，同为 9:00）走 re-affirm 把它降回
  ACTIVE；若已 RESOLVED 则走 ESTABLISH 复活新 episode。修复两层：
- `reconciler._is_stale_creation`：把「仅 replay 标记才拦截等时间戳」放宽为
  「任何 observation 在同一或更晚语义时间已被不同 observation 占有该 key 即拒绝
  创建/复活」（等时间戳的异步重抽取同样拦截，含非 replay 场景）。
- `service._process_async`：worker 新抽取的 observation 在 store 重载后重新打
  is_replay 标记（瞬态字段不随重载保留）——迟到分析套用严格同/晚时间戳占有规则，
  re-affirm/UPDATE 不再能把状态降级或复活。
- D8 压测 15/15 通过（修复前 4-8/15 失败）。回归测试：TestV2ResurrectionGuard×3
  （症状/计划等时间戳重分析不复活；严格更新的新声明仍开新 episode）。

### 2. [迁移] R9 显式语言优先 → inference 统一证据优先级（V2 §22）

- 新增 `InferenceEngine._infer_r9`：任何 CONFIRMED 且带 key 的 observation 都会
  对同 key 的 INFERRED/ESTIMATED 非终态状态发 UPDATE 升级 intent（与原 V1
  `_apply_r9` 守卫一致）；`infer()` 对所有分支（interaction/plan/symptom/
  activity/未接管）统一追加。
- `reconciler.apply()` 从「owned→intents / 非 owned→V1」二选一改为双路径：
  非 owned 先跑 V1 fallback，再执行 intents（保持「handler 后显式 supersede」
  的历史顺序）。
- 删除 V1 `_apply_r9` 及其调用；`_infer_plan` 内部 R9 块并入统一规则。
- 顺带修复 round5 潜在 bug：UPDATE 类 intent 未显式设 certainty 时，dataclass
  默认值 'observed' 会被写入 canonical state（cancel/resolve/complete/re-affirm
  全部中招）——这些 intent 现在显式 `certainty=""`（既有约定：空串=不改）；
  symptom re-affirm 在 CONFIRMED 声明下合并 R9 升级。
- 迁移验收：TestV2R9EvidencePrecedence×3（未接管 observation 的 R9 intent、
  CONFIRMED 症状升级 INFERRED certainty、V1 `_apply_r9` 已删除）。
- 全套 **116 passed = 110 基线 + 新增 6**（R10 1 + ResurrectionGuard 3 + R9 3），
  0 failures；V1 D1-D12/安全/MCP stdio 全部保持绿。
- 剩余 V1：R1/R2（显式睡眠/清醒语言，P3 测试钉死 V1 所有权）、R4 活动半部、
  R10（lazy_expire 窗口机制）、S1/S2 系统规则。
