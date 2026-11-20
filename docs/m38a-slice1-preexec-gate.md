# M38-A slice 1 — 执行层接管：pre-exec gate

总单 surlymochan/swrm#288，跟进单 #292。只开 PR 不合并，待主轮第二方复核。
切片 1 = 执行层接管核心；真实 run 证据（批准→执行、拒绝→不落地、超时
未决→不执行，时间戳证明决定先于执行）与总验收 #288 在切片 2。

## 接管点

`configure_executor_environment()`（bin/openswarm）＋ `LaneChild.__init__`：
门开启时把 gate shim 装进 `<worktree>/.openswarm-tmp/bin/` 并把该目录
prepend 到执行器子进程 PATH 首位。后端经 shell 执行的每一条高危命令
（`git` / `npm` / `gh` / `rm` / …）都先落到 swrm 自有的 shim；shim
分类、建待审批记录、等 `openswarm approval approve|reject` 裁决，批准
后才 `exec` 真实二进制，拒绝/超时则 exit 130 且不执行。

为什么是这个点（三次裁定已证伪其余点，见 #292 终裁）：

- 事件循环 `item/started`：codex 先执行、后发事件，push 在决定前已落地
  （#294：push 比决定早 22–26 秒）；它是通知，无回执可拦。
- `approvalPolicy=on-request`：codex 0.166.0 只对 `rm -rf` 类发原生
  `requestApproval`；push/publish/日常命令 0 次且先落地（8 形侦察）。
- 沙箱收紧：只是在执行层更早挡住写操作，不让 codex 开始发请求。

每轮 arm：`lane_run_turn` 在 `lane_begin_turn` 之前写
`.openswarm-tmp/gate.json`（run_id、类别、wait、deadline、approvals 索引/
run 文件/ledger/事件文件路径）；`lane_do_run` finally 与 `exec_hermes`
finally disarm。gate.json 缺失/过期/门关时 shim 直接 exec 真实命令。
arm 时门开则先重装 shim（幂等）：`lane_prepare_workspace` 每轮派单都
跑 `git clean -fdx`，会删掉 supervisor 启动时装的 shim，只靠 arm 前
检查 bin/ 是否存在、门在真实 lane run 中永远无法 armed（#296）。

## 做了什么

- **新文件 `bin/openswarm-gate-shim`**（stdlib-only，不 import openswarm：
  实测 `bin/openswarm --help` 需 0.7 秒，每条 shell 调用都付不起）。
  分类正则与 D3 脱敏从 `enforced_risk` / `DEFAULT_RISKS` /
  `redact_command(evidence=True)` 逐字镜像，一致性由测试语料锁定。
  （注意：secret 规则须含 `enforced_risk` 追加的 `.env` 文件分支，只抄
  `DEFAULT_RISKS[3][0]` 会漏判 `cat .env`。）
  shim 动词：git、npm、pnpm、yarn、twine、gh、rm、sudo、cat、less、more、
  head、tail、bat、printenv。
- **记录链复用切片 1**：待审批记录仍是 run `approvals.json`＋全局索引＋
  ledger＋事件文件四处落盘，形状与 `request_approval` 一致（新增
  `command_key` 字段，供双向去重）；`openswarm approval list/approve/
  reject` 不改。`request_approval` / `decide_approval` 的索引读写加
  `_approval_lock`（与 shim 共用同一锁文件），防并行 shell 建单与 CLI
  裁决互相丢记录。
- **双向去重**（同一动作只判一次）：批准记录带 `command_key`（token 级
  归一化键；`zsh -lc` 包裹、`sudo`、`env` 透明包装、引号差异均归一）。
  同一 run、同一 key、120 秒内已有 approved——shim 直接放行（loop 先批
  的原生请求亦然），事件循环遇到已批的 `item/started` / 原生请求记
  `preexec_resumed` 后继续，不二次暂停。同一 key、600 秒内已有 rejected
  ——shim 立即 exit 130，不再建单（防 codex 重试刷单挂住 lane），只记
  `approval_rejected_repeat` 事件。
- **中止竞态**：`lane_abort_child` 只发 `turn/interrupt`（礼貌请求），
  循环原被动等 30 秒；实测 codex 30 秒不退。改
  `ABORT_STOP_GRACE_SECONDS = 5`——中断发出后 5 秒仍活着即
  `child.close()`（kill_tree：SIGTERM→1 秒→SIGKILL 全进程树）并立即返回
  stopped/risk_blocked（130/133）。拒绝本身不需 abort：高危命令根本没
  启动，shim exit 130 即跳过该动作。
- **门关逐字节一致**：`approval.enabled` 仍默认 false；门关时不装 shim、
  PATH 不变；one-shot `run_proc` 不装 shim（沿用原有 enforcement block）。

## 已知缺口（明写，不藏）

- `echo`/`printf` 是 shell 内建，不走 PATH，其 secret-print 仍只走事件
  检测（事后记账）。
- 绝对路径调用（如 `/usr/bin/git push`）绕过 PATH shim；事后仍由事件
  循环记账（`risk_blocked`），但拦不住。
- applyPatch/fileChange 类文件写不走 shell，不在本切片内（#288 四类无此项）。
- Windows 装 `<verb>.cmd` 包装（调同一 Python shim）；本机无 Windows
  实测，CI 覆盖生成逻辑。

## 测试

新增 `tests/test_m38a_slice1.py`：分类一致性（shim == approval_risk）、
key 一致性（shim argv key == loop key，含包裹形）、脱敏一致性、shim
端到端（门关直通、批准后执行且 decided_ts < 执行时刻、拒绝/超时不
执行且 exit 130、重复拒绝快 fail、已批复用、真实二进制解析、缺失
二进制 127）、安装/arm/disarm、loop resume、abort 宽限 5 秒。
全量：`python3 -m pytest tests/ -q` —— 见 PR CI；本地一次跑完结果见本单。

## 未做（切片 2）

真实 run 证据：门开的 codex lane 上 ≥1 批准后恢复完成＋≥1 拒绝后未
执行高危动作，时间戳复核决定先于执行；门关对照 run 一次；M38 总验收。
