# M12 slice 2 (#148): 真实 fleet 回收证据

执行时间：2026-11-11 22:21–22:22 SGT（UTC 14:21–14:22）。命令为本切片
PR 的工作分支构建（`bin/openswarm`，未发版）。证据家目录（原始
status.json / ledger / 逐轮输出原文）保留在本机：
`~/workspace/github-maturity/bench-env/m12-s2-evidence-home/`，其中
`evidence/01–06` 为六份原始输出；本文件为归档摘要，数字全部引自原文。

## 证据 run 的构造（真实 pid，非伪造）

三个 run 均以真实进程做 runner，身份由 `/proc/<pid>/stat` 起始时间戳定：

| run | runner_pid | 构造 | 判定依据 |
| --- | --- | --- | --- |
| `m12s2-dead` | 3049（`proc:71028`） | 真实 `sleep 0.2` 进程，记录 pid 与起始时间后已退出 | pid 不存在 → dead |
| `m12s2-alive` | 3050（`proc:71048`） | 真实 `sleep 900` 进程，证据采集时在跑；events 新鲜 | pid 存在且起始时间匹配、活动 0.3 分钟前 → alive |
| `m12s2-zombie` | 3051（`proc:71049`） | 真实 `sleep 900` 进程，证据采集时在跑；events 30 分钟未动 | pid 存在且起始时间匹配、无活动 30.3 分钟（≥10）→ zombie |

三 run 的 ledger `started` 行分别为 1 / 2 / 3 行。证据采集后两个
sleep 进程已 kill（pid 3050、3051 均 gone）；此后对该家目录重跑
health 会把 alive/zombie 判为 dead——本文件所贴输出为采集时刻的证据。

## 1. 回收前：`fleet health`（只读）与人工逐行核对

```
local  reachable  alive=1 dead=1 zombie=1 unknown=0
  run m12s2-alive alive pid=3050 (match) idle=0.3m last=2026-11-11T14:21:05Z ledger:2
  run m12s2-dead dead pid=3049 (gone) idle=30.3m last=2026-11-11T13:51:05Z ledger:1
  run m12s2-zombie zombie pid=3051 (match) idle=30.3m last=2026-11-11T13:51:05Z ledger:3
```

人工核对（独立于工具输出的 shell 实查，见 `evidence/03`）：

- pid 3049 GONE；pid 3050 EXISTS（`/proc` 起始时间 71048，与记录的
  `proc:71048` 一致，cmd `sleep 900`）；pid 3051 EXISTS（71049，
  与 `proc:71049` 一致，cmd `sleep 900`）。
- events mtime：alive 14:21:05Z（新鲜）；dead / zombie 13:51:05Z
  （30 分钟前）。
- `grep -n run_id ledger.jsonl`：1 行 dead、2 行 alive、3 行 zombie，
  与输出的 ledger 指针逐行一致。
- 三 run 判定与 `fleet health --json`（`evidence/02`）逐行一致。

## 2. 回收：`fleet health --reap`（显式触发）

```
local  reachable  alive=1 dead=1 zombie=1 unknown=0 reaped=1 skipped=2
  run m12s2-alive alive pid=3050 (match) idle=0.6m last=2026-11-11T14:21:05Z ledger:2
  run m12s2-dead dead pid=3049 (gone) idle=30.6m last=2026-11-11T13:51:05Z ledger:1
  run m12s2-zombie zombie pid=3051 (match) idle=30.6m last=2026-11-11T13:51:05Z ledger:3
  reaped m12s2-dead dead -> exited ledger:4
  skip m12s2-alive alive (only dead runs are reaped)
  skip m12s2-zombie zombie (runner alive; M11 reap only applies to dead runners, so a zombie is reported, not reaped)
```

回收后对照（见 `evidence/06`）：

- `m12s2-dead/status.json`：`state=finished`、`status=exited`、
  `reaped=dead-runner`、`exit_note=runner process gone (pid identity
  checked, #98)`、`finished=1794406902.78`。
- ledger 第 4 行（回收记录原文）：
  `{"id": "reap-m12s2-dead-1794406902", "run_id": "m12s2-dead", "ts": 1794406902.78, "event": "reap", "fleet_health_reap": true, "status": "exited", "reaped": "dead-runner", "reaped_by": "fleet health --reap", "verdict": "dead", "runner_pid": 3049, "pid_identity": "gone", "title": "M12 slice 2 evidence: dead runner"}`
- 回收后 `fleet health`：`alive=1 dead=0 zombie=1`，dead run 已不在
  非终态列表（计入 finished_runs）。
- alive / zombie 的 `status.json` md5 与回收前完全一致
  （alive `6e56279b…`、zombie `379f1705…`），即未被触碰。
- `watch m12s2-dead`：立即走 DONE 分支退出（rc=0），
  `[2-dead 14:21] DONE: scope=repo status=None backend=None/None …`，
  不再重发 BUDGET/STALL。`watch m12s2-zombie`：25 秒超时时仍在等待
  （timeout rc=124），即未被标记 finished、仍在被 watch。
- 幂等：紧接着再跑一次 `fleet health --reap --json`，`reaped=0`、
  `skipped=2`，ledger 仍为 4 行，无重复回收记录（见 `evidence/05`）。

## 3. 需主轮裁定的设计点：zombie 不回收

`--reap` 只回收 dead 判定；zombie（runner 活着、只是无活动）明确
跳过、只报告。理由：M11 回收的守卫条件就是 runner 已死（`runner_alive`
为真时 `reap_dead_run` 返回 False）；把一个活着的 runner 的 run
标记 finished，会让活进程继续写一个已终态的 run，状态自相矛盾。
zombie 仍由 watch 继续发 STALL 告警，留待人工处置。本切片的真实
fleet 证据因此是 1 个 dead 回收（`m12s2-dead`）＋ 1 个 alive 健康判定
（`m12s2-alive`），外加 1 个 zombie 跳过实证（`m12s2-zombie`）。
若主轮认为 zombie 也应可回收，需另定“先停 runner 再标记”的语义，
不在本切片范围内。
