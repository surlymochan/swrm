> Repo copy of the swrm × dsh-superagent fusion plan (landed with M27 slice 1, #213). Source of truth for the plan text is the goal ledger; this copy is the in-repo reference. Upstream: `deepseek-ai/dsh-superagent-prototype` (MIT) — only the domain model is ported, no upstream code is vendored; closing still requires swrm verify + evidence, not executor self-report.

# swrm × dsh-super-agent 融合方案

立项：2026-11-14 19:35 用户亲令「swrm 先办法把 dsh-super-agent 融合进来」。
对象核实（2026-11-14 gh 实查）：`deepseek-ai/dsh-superagent-prototype`（DeepSeek Harness 团队 2026-10-31 开源，23,094 star，TypeScript，**MIT**，README 自述 research prototype）。市面另有多个同名社区仓（vicfei、TianzeLuo 等），均非官方，不作融合对象。

## 一句话结论

不搬代码、搬领域模型：dsh-superagent 是一套「自治接入层」（监听 → 准入研判 → 授权工单 → 派工 → 回贴 → 续接），实现绑死 DeepSeek Harness 会话、单仓单机；swrm 恰好有它缺的「真调度层」（异构 backend、额度路由、健康门控、M26 远程 runner、verify 证据结单），又恰好缺这套接入层（swrm 现在只能被动执行派给它的任务）。融合 = 把它的领域模型用 Python 移植到 swrm 上层，swrm 现有派单当它的 Executor 层。官方文档 001 自述「概念与规则可复用，Source / Policy / Executor / Channel 实现属于部署」——这正是官方留的融合口。

## 对方有什么（实读 docs/000–008 与 src/ 布局）

领域流：`Source → Trigger → Thread → Eval（Gate 确定性准入 + Assessment 模型研判）→ Decision（accept / stop / ignore / unavailable）→ Ticket + Mandate（授权：policy 版本、scope、strength、命中规则、来源身份，不可变）→ Assignment（绑定一个 Executor 实例，可多次）→ Executor 执行 → Report → Channel 回贴来源线程`。

关键机制：
- **Source**：事件流（GitHub notifications / issue / PR / CI、Feishu），自持读位置（重启不重放）、过滤 bot 自身消息、把事件翻译成 Trigger 与 Thread。
- **Gate**：按 Source 的 Policy（allowlist + admission 表：接收范围 × 在名单/未在名单 × 是否 @ → explicit / inferred / ignore）做确定性判定；bot 名单内的机器人最多产出 inferred 候选，永远不能 explicit。
- **Assessment**：第二层模型研判，无状态，输入 Trigger 批次 + Thread 快照 + 该 Thread 全部 Ticket + 当前 Policy。
- **Mandate**：接单即固化的授权凭证；扩 scope = 新 Ticket；只有真人 stop 或 policy 失效能撤；resume 前复核当前 policy。
- **Subscription**：Ticket 订阅其 Thread，后续消息直接 fan-out 给 Ticket、不重过 Eval；真人 follow-up 可 resume 已结束的 Ticket（同 Mandate）；bot / CI 消息只作上下文、不触发 resume。
- **Attention / Panel**：网页面板列「需要真人处理」的事项，贯彻其哲学——占用真人注意力越少越好。
- 实现形态：TypeScript 单体（core engine / evals / tickets / assignments、sources/github|feishu、executor/dsh 会话每 Ticket 一个目录、kv store、panel）。

## swrm 现状（实读仓库）

Python 3.9+、stdlib-only、零依赖；核心 `bin/openswarm` + fleet / broker 桥；`backends/*.json` 描述异构执行器（codex、pi、deepseek、ocgo、cursor、copilot、hermes、agy）；`policy.json` 是额度/价格/路由策略（与准入 Policy 不是一回事，融合时不得混名——准入策略另立 `intake-policy`）；已有 claim-under-lease 派单、quota 路由、健康门控（M24/M25）、verify 证据结单、M26 远程 runner（切片 1 已合，切片 2 在飞）。

swrm 缺的正是接入层：没有事件监听、没有「谁有权让我干活」的准入、没有跨会话的工单与授权概念、任务结束即结束（无续接）、结果只回调用方不回来源线程、没有待真人处理的收件箱。

## 融合映射（落点）

| dsh-superagent | swrm 落点 |
| --- | --- |
| Source / Trigger / Thread | 新增 `intake` 子命令族：GitHub Source 第一版走 gh 轮询（读位置落盘、去重、丢 bot 自身消息）；Feishu 源留接口、第二期 |
| Eval · Gate | 确定性准入层：allowlist + admission 表 + accept mode（off / shadow / on），Decision 全部落账 |
| Eval · Assessment | 模型研判第二层：复用 planner 同款调用，输出 accept / ignore + 理由，写入 Decision |
| Mandate | Ticket 创建时固化（policy 版本、scope、strength、命中规则、来源身份），不可变 |
| Ticket | 新增工单层：状态 open → ended（可 resumed），独立于单次 run；一个 Ticket 可有多个 Assignment |
| Assignment | 直接复用现有 batch / fleet / M26 远程派单（claim under lease 不变） |
| Report / 结单 | **刻意不照搬**：dsh 靠执行器自报 ready 结单；swrm 沿用 verify + 证据结单（2026-11-07 既定分歧点） |
| Subscription | Ticket 订阅来源 Thread，新评论 fan-out 成 follow-up 输入；真人消息 resume、bot / CI 只作上下文 |
| Channel | gh 评论回贴来源 issue / PR，附 run 证据 |
| Panel / Attention | 第一版做 `attention` CLI 列表（waiting / blocked 的 Ticket），不做 web 面板 |
| Executor（DSH 会话） | 不引入 DSH 本体；若目标机装有 dsh CLI，可后续加 `backends/dsh.json` 把它当一个异构 backend（可选，不进本 epic 验收） |

## 里程碑切分（swrm M27，epic）

- **切片 1 · 领域核心 + 账本 + Gate（shadow）**：五概念数据模型与状态机、intake 账本（append-only）、幂等（同一 Trigger 重复投递只建一个 Ticket）、policy 版本化 + admission 表 + Gate；shadow 模式只记 Decision 不派单。用本仓真实 issue 评论流做回放 fixture 验 Decision 正确性。
- **切片 2 · GitHub Source + Assessment + 真派单 + Channel**：gh 轮询 Source、模型研判、accept → Ticket → 现有派单 → 结果回贴；accept mode 默认 shadow，验收时只对受控仓（surlymochan/swrm）翻 on：授权账号在 issue 里下达小任务 → 自动接单、真派单、回 PR + 评论；未授权账号请求 → ignore 落账。
- **切片 3 · Subscription 续接 + attention + 验收总成**：fan-out、ended Ticket 真人 resume（同 Mandate、复核 policy）、真人 stop 撤回 Mandate、`attention` CLI；三案例（接单 / 拒绝 / 续接）真实证据齐后验收发版，README / NOTICE 注明 MIT 来源与结单差异。

epic 验收四条见总单（领域落盘可复核 / 真实端到端接单 / 续接案例 ≥1 / 安全默认与不回归）。

## 边界与红线

- 排期守里程碑制：M26 收口（切片 2 在飞 → 切片 3 验收发版）后 M27 第一顺位开工，不并行两 epic。
- 不动 CLI 名 `openswarm`、bin 路径、`OPENSWARM_*` 环境变量与既有子命令签名 / SKILL 契约；新能力走新子命令。
- stdlib-only 不变；不引入 DSH 运行时依赖。
- 安全默认：shadow 起步、只对受控仓翻 on；allowlist 外一律 ignore 并落账；涉钱 / 对外承诺类请求即使来自授权账号也只接单生成草案、不自动执行（沿用全线对外口径）。
- 许可合规：MIT——移植时保留版权声明、README 致谢并注明改动点（结单语义）。
- 自有仓工作，不计入 500 PR 冲刺计数。
