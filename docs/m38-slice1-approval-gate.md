# M38 slice 1 — 审批门核心：policy＋暂停/恢复握手＋ledger＋CLI

总单 surlymochan/swrm#288，切片 1 子单 #289。只开 PR 不合并，待主轮第二方复核。

## 做了什么

- **policy**：`policy.json` 新增 `approval` 节（默认 `enabled: false`，类别
  `push / publish / destructive / secret` 全开，`wait_seconds: 1800`）。
  关闭时不拦截任何动作，行为与今天逐字节一致（`risk.enforce` 仍按原样硬拦截）；
  可在 `~/.claude/openswarm/policy.local.json` 覆盖。
- **分类复用现有检测**：`approval_risk` / `approval_gate_for_block` 直接调用
  `enforced_risk`（同一批正则、同一份 redaction），再把 finding 名映射到四类
  （push→push、publish→publish、rm outside worktree/sudo→destructive、
  secret print→secret）。`git reset --hard / checkout -f / clean -f` 不是
  enforced risk，复用 watch 的 `DEFAULT_RISKS[0]` 同一条正则补上，归入
  destructive，不另起检测。`bypass/force flag`、`keychain access`、
  `computer use` 不在四类内，仍走 enforcement。host scope 的 warning
  （`enforced: false`）不进审批门。
- **暂停/恢复握手**：`lane_run_turn` 在 permission-request 协议消息
  （ACP `session/request_permission`、Codex `item/commandExecution/requestApproval`、
  `execCommandApproval`）上，命中已开类别时先 `request_approval` 记录待审批项，
  再 `wait_for_approval` 等待 `openswarm approval approve|reject`：
  批准后用该协议已有的批准回执（ACP selected option / `accept` / `approved`）
  回复并从暂停点继续循环；拒绝或超时则发拒绝回执并落回原有 enforcement
  block（动作不执行，`risk_blocked` 133）。`fileChange`/`permissions` 等
  无已知批准回执形状的方法、以及无 permission-request 通道的一次性 `run`，
  保持原有硬拦截不变。
- **ledger**：待审批记录三处落盘——运行目录 `approvals.json`、全局
  `$OPENSWARM_HOME/approvals.json` 索引、主 ledger 一行
  （`type: approval_requested/approval_approved/approval_rejected`，
  含 `approval_id / approval_category / approval_risk / command` 证据指针与
  `pause_point`：run id、request id、method、command）。ledger 行无 backend，
  路由统计读取方按 backend-less 跳过，不计入 run 计数。
- **CLI**（只新增，不改现有子命令签名）：
  `openswarm approval list [--status pending|approved|rejected|all] [--run ID] [--json]`、
  `openswarm approval approve|reject <id> [--by NAME] [--reason TEXT] [--json]`。
  未知 id 或重复裁决报 `ValueError`、exit 2。
- **文档**：README 风险段后新增审批门段落；SKILL.md 风险行追加一句。

## 默认关闭未变（证据）

- `tests/test_m38_slice1.py::test_default_policy_gate_is_off`： shipped
  `policy.json` 的 `approval.enabled` 为 false；关闭时 `git push` 事件
  `approval_risk` 返回 None。
- 全量套件一次跑完（见下），无回归。

## 测试

- 新增 `tests/test_m38_slice1.py`：16 项（9 个 subtests），覆盖默认关闭不拦截、
  四类映射、类别子集、host warning 不进门、request/decide/list 三处落盘与
  ledger 行、重复裁决/未知 id 报错、wait 超时、四种回执形状、ACP 选项选择、
  pause→approve 恢复（线程内裁决）、pause→reject 跳过、pause 超时记为
  rejected（reason=timeout）、CLI list/approve/reject。
- 全量：`python3 -m pytest tests/ -q` —— 见 PR CI；本地一次跑完结果见本单评论。

## 未做（切片 2）

真实 run 证据（≥1 批准后恢复完成＋≥1 拒绝后未执行高危动作）与总验收 #288。
