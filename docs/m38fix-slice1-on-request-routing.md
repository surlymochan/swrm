# M38 follow-up slice 1 — on-request handshake + non-gated disposition + evidence redaction

修补单 surlymochan/swrm#292，切片 1 子单。只开 PR 不合并，待主轮第二方复核。
设计：#292 评论 7019502431（2026-11-20 03:08 轮主轮定稿，D1–D4）。

## 做了什么

- **D1 策略翻转**：`codex_approval_policy()` 按门开关取值，`lane_begin_turn` 的
  codex `thread/start` 由硬编码 `"never"` 改为门开时 `"on-request"`、门关时
  `"never"`。其他后端（pi/cursor/hermes/copilot）与 one-shot `run`（`exec_codex`
  仍 `-c 'approval_policy="never"'`）不动；`approval.enabled` 仍默认 false。
- **D2 非门控处置（单一路由 `lane_approval_route`）**：事件主循环的 codex 分支
  与 `wait_response` 两处调用点统一走该路由。顺序：①强制风控硬阻塞（调用方原有
  分支不变）；②门控分类暂停等人裁决（调用方原有分支不变）；③host 警告一次放行
  （仅命令请求，fileChange/permissions 仍拒绝，与现行一致）；④非门控命令请求按
  路由表自动放行/拒绝——安全集：只读 git（status/diff/log/show/branch/rev-parse/
  ls-files）、只读查看（ls/pwd/cat/head/tail/grep/rg/wc/echo/printf）、工作树内测试
  与构建（pytest/npm test/go test/cargo test/bun test/vitest/jest/tsc/eslint）、工作
  树内 git（add/commit/checkout（不带 -f/--）/branch/stash/tag/merge）；禁 sudo、
  网络动词（curl/wget/ssh/scp/rsync）、工作树外路径、rm、reset/rebase/clean、工作
  树外重定向；任一 invocation 不满足即拒绝并记事件 `approval_declined_non_gated`
  （含命中规则与脱敏后命令）；命令为空或解析失败亦拒绝。⑤非门控 fileChange：
  全部目标路径 realpath 落在工作树内放行（回执 `{"decision": "accept"}`，切片 2
  探针实测确认形状）、任一越界拒绝并记事件。⑥permissions 与未知方法维持现状拒绝
  （permissions 仍回空集）并记事件。⑦默认拒绝。每次自动放行/拒绝都在事件文件留
  痕（方法＋脱敏命令＋理由，`approval_auto_approved` /
  `approval_declined_non_gated`）。
- **D2 调用点**：`wait_response` 内 codex 后端的权限请求不再当场拒绝，改为放回
  deferred 队列、由事件主循环按同一路由处理；非 codex 后端维持现状。门关时路由
  保持现行 blanket decline（门关行为与今日一致）。
- **D3 证据脱敏**：`redact_command(command, evidence=False, worktree=None)`；
  evidence=True 时保留决策相关操作数——`git push` 的 remote 与 refspec、
  `npm/pnpm/yarn publish` 的 `--tag` 值、`gh release` 的子命令与 tag；其余仍脱敏。
  脱敏规则：含 `://` 的 token 只留 `scheme://host`（去 userinfo）；命中已知密钥
  前缀（`sk-`/`ghp_`/`gho_`/`AKIA`/`xoxb-` 等）的一律脱敏；工作树外绝对路径脱敏
  （无 worktree 时 fail-closed）。证据命令只进 gate 记录：`enforced_risk` 的
  finding 仍为全脱敏（`risk_blocks` / result.json 不泄漏操作数——`test_lane.py`
  的 `secret-value` 断言仍成立）；`approval_risk` 从事件原文重算触发命令的证据
  形式后写进 gate 记录，事件主循环改走 `approval_risk` 统一取 gate；
  `_approval_git_destructive` 的 gate 记录直接用 evidence 模式。审批记录另存
  `event_ref`（事件文件＋行号指针，`request_approval` 写入时计数），ledger 行
  同步携带；完整命令仍只留在事件文件，不进 approvals.json/ledger。
- **设计外判断（两处，待切片 2 探针验证）**：① `cd` 不在设计安全集内，但
  `bash -lc "cd <dir> && ..."` 是 codex 的常规命令形状，工作树内的 `cd` 按
  「只改 cwd、后续 invocation 逐个核查」放行，工作树外 `cd` 拒绝；②透明包装
  （`sh -c`/`env`/`timeout`/`nice`/`exec`/`command`/`nohup`/`!`）本身不视为动作，
  以被包装的 invocation 为准（`sudo` 除外，仍硬拒绝）；`2>/dev/null` 等
  `/dev/*` 重定向目标放行。③ `fileChange` 的 `accept` 回执形状为首选、待切片 2
  实测确认；若形状不对，改回拒绝并记账。
- **文档**：README 审批门段落、SKILL.md 审批行、policy.json `approval._doc`
  同步新行为；`approval.enabled` 仍默认 false。

## 默认关闭未变（证据）

- `tests/test_m38fix_slice1.py::test_shipped_default_still_never`：shipped
  `policy.json` 的 `approval.enabled` 为 false；门关时 `codex_approval_policy`
  返回 `"never"`，路由对 `git status` 仍 blanket decline。
- `tests/test_m38fix_slice1.py::test_redact_default_mode_is_unchanged`：
  非 evidence 模式输出与旧版逐字节一致。
- 全量套件一次跑完（见下），无回归。

## 测试

- 新增 `tests/test_m38fix_slice1.py`：28 项（43 个 subtests），覆盖 D1 策略
  （on-request/never、shipped 默认、lane_begin_turn 实发值）、D2 路由表每条分支
  （放行 17 例、拒绝 19 例、空命令/解析失败、exec 形状、门关 blanket decline、
  非权限消息不处理、host 警告仅命令放行、fileChange 树内/越界/无路径、
  permissions/applyPatch 拒绝、fileChange 回执形状）、`wait_response` 延期
  （codex 放回 backlog 且不发拒绝、非 codex 仍当场拒绝）、事件主循环接线
  （安全命令 accept、危险命令 decline、树内 fileChange accept，轮次继续不 abort）、
  D3（默认模式不变、push/publish/release 保留、密钥 URL/前缀脱敏、工作树外路径
  脱敏、`approval_risk` 命令保留、`request_approval` 的 `event_ref` 与 ledger 携带）。
- `tests/test_m38_slice1.py` 一处断言随设计变更更新：fileChange 回执不再是
  None，改为 accept/decline（新断言见本测试文件）。
- 全量：`python3 -m pytest tests/ -q` —— 见 PR CI；本地一次跑完结果见本单评论。

## 未做（切片 2，另派）

- 真实 run 证据：① M38 切片 2 同构两案重跑（批准恢复＋拒绝未落地）；② 非门控
  流量探针（门开启的 scratch codex lane 日常工作，统计自动放行/拒绝、零因非门控
  误拒导致的 lane 卡死；并验证 `cd`、透明包装、`/dev/null` 重定向三处设计外判断
  与 fileChange `accept` 形状）；③ 门关对照 run。①②③ 全过才算 M38 验收。
- 已知风险（设计已载明）：`wait_response` 延期假设 codex 不在 turn/start 回复
  前等权限回执；若探针发现死锁风险，立即改回当场拒绝并记账。
