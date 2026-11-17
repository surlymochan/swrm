# M31 slice 2: 真实跨主机证据 + health-gate 默认翻转再评估 (swrm #256)

Refs: 总单 #253，切片 1 #254 (PR #255, `efd5bbf`)。
本文档是证据记录，不宣告验收——验收由主轮第二方复核裁定。
所有数字均从 gocc 直跑探针、控制端账本、远端 run 的 `result.json` /
远端账本回读，从无执行方自述。

## 0. 结论先行

- 真实跨主机 fleet run：3 次（deepseek 拦下 1、echo 放行门控但执行卡住 1、
  codex 放行且真实执行完成 1）。拦/放两端均有账本行，满足“≥2 个、
  ≥1 拦下＋≥1 放行”。
- 翻转评估：**不翻转**（见 §5）。跨主机证据本身过关，但裁定标准第一条
  “默认开启全量绿”不通过：默认改为 `enabled: true` 后全量套件
  1345 例中 55 失败＋9 错误（§4）。按 #256 正文“证据不过关则结论落单、
  不翻转”，`policy.json` 保持 `enabled: false`，README / SKILL.md /
  代码注释均不改。本 PR 只含证据文档与评估测试，不含翻转改动。
- CHANGELOG 说明：仓内无 `CHANGELOG.md`（全仓检索无此文件；发版说明走
  GitHub Release，由发版步骤写）。不翻转故无同步项；翻转若将来重提，
  须把翻转写进 Release notes。

## 1. 实测配置

- 分支二进制（控制端与 gocc 同款，`main efd5bbf` + 本切片工作区）：
  控制端 `~/workspace/github-maturity/bench-env/ownrepo-1117-swrm-m31s2`，
  gocc `/tmp/swrm-m31s2`（rsync 部署，`bin/openswarm` + `policy.json`；
  另拷切片 1 的 `backends.json` / `task.md` / `echo-cli.py`）。
- 控制端 home（隔离，`/tmp/m31s2-fleet-ctrl`）：`fleet.json` 含 gocc ssh
  host（`openswarm: /tmp/swrm-m31s2/bin/openswarm`），`state.json` 为
  `tests/fixtures/state.json`（控制端 codex 不健康，见 §3 分歧实证）。
  `OPENSWARM_SSH_CONFIG=/home/hatch/.ssh/config`（本 VM 以 root 运行，
  裸 ssh 读空的 `/root/.ssh/config`，无 gocc 别名；见 §6 误启动）。
- 远端工作仓：gocc `/tmp/swrm-m31s2-work`（新建小 git 仓，仅 README）。
  任务卡：`Write a one-line summary of this task card. No code changes.`
- 门控姿态：切片 1 证据阶段用 `--health-gate` 旗标开启（与切片 1 同口径）；
  翻转后用默认开启重验（§5），不再带旗标。

## 2. 探针基线重测（gocc 直跑，本切片新测，未沿用切片 1 旧数）

2026-11-17 约 21:2x SGT，`python3 /tmp/swrm-m31s2/bin/openswarm
health-probe --backend <b> --json`：

| backend | verdict | reason | checks |
| --- | --- | --- | --- |
| deepseek | unhealthy | no DeepSeek API key (DEEPSEEK_API_KEY unset, no keychain item) | binary pass (`/Users/surlymo/bin/opencode`) / login fail / quota unknown |
| codex | healthy | all 3 applicable check(s) passed | binary pass / login pass (live session) / quota pass (remaining 0.32999999999999996) |
| echo（无 backends 文件） | unknown | no probe state for backend 'echo'; binary presence could not be judged | binary unknown |
| echo（`--backends-file /tmp/swrm-m31s2/backends.json`） | healthy | all 1 applicable check(s) passed | binary pass (`/usr/bin/python3`) |

三条 verdict 与切片 1 基线一致，但均为本切片重新实测。

## 3. Fleet run 证据（控制端账本 `/tmp/m31s2-fleet-ctrl/ledger.jsonl`）

| # | 命令 | 结果 |
| --- | --- | --- |
| B1 拦下 | `fleet exec --health-gate gocc -- run --backend deepseek --task-file /tmp/swrm-m31s2/task.md --cwd /tmp/swrm-m31s2-work` | RC=2，`health gate blocked dispatch: backend 'deepseek' is unhealthy — login: no DeepSeek API key (DEEPSEEK_API_KEY unset, no keychain item)` |
| A1 放行（echo） | `fleet exec --health-gate gocc -- run --backend echo --backends-file /tmp/swrm-m31s2/backends.json --task-file /tmp/swrm-m31s2/task.md --cwd /tmp/swrm-m31s2-work` | 门控放行，但远端执行卡住（见下）；控制端 RC=124 transport timed out（300s） |
| A2 放行（codex，真实执行） | `fleet exec --timeout 600 --health-gate gocc -- run --backend codex --task-file /tmp/swrm-m31s2/task.md --cwd /tmp/swrm-m31s2-work` | 完成：远端 run `20261117-213237-5602ef`，28.848s，见下 |

控制端账本 `fleet_health_gate` 行（3 条，逐条与 gocc 直跑探针一致）：

- B1：`status blocked`，`backend deepseek`，`verdict unhealthy`，
  `probe_host gocc`，`probe_side executor`，reason 与 §2 deepseek 行逐字一致，
  checks 三条逐项一致。无 exec 尝试（门控在转发前拦下）。
- A1：`status dispatched`，`backend echo`，`verdict healthy`，
  `probe_host gocc`，`probe_side executor`，`action allowed`。
- A2：`status dispatched`，`backend codex`，`verdict healthy`，
  `probe_host gocc`，`probe_side executor`，`action allowed`；
  checks 与 §2 codex 行逐字一致（含 quota remaining 0.33）。

分歧实证（控制端状态不裁决远端）：控制端本机探针 codex 为 unhealthy
（`codex is not logged in (codex login status says 'Not logged in')`，
binary `/opt/hatch-image/bin/codex` 可执行、login fail），gocc codex
为 healthy，A2 仍放行——裁决用的是执行端探针，与切片 1 结论一致。

A2 远端执行（gocc `~/.claude/openswarm/runs/20261117-213237-5602ef/`，
`result.json` 与远端账本尾行互核）：`status partial`，
`backend codex`，`model gpt-6-luna`，`effort high`，`seconds 28.848`，
`queued_seconds 0.102`，`result.status done`（`Summarize the task card
in one line without making code changes.`），`files_changed []`，
usage `input_tokens 25838 / output_tokens 122 / reasoning_output_tokens 71`，
`quota_before 0.67 → quota_after 1.0`（used 口径）。
`partial` 的原因是任务本身要求 no code changes，故无 branch/commit
（`verify_note: no_change_evidence`），不是派单失败：执行器 rc=0、
result done、28.8 秒真实模型调用。远端账本尾行 id 与 run id 一致，
status partial、28.848s。

## 4. 默认开启全量套件（翻转后，分支上实测）

分支在 `policy.json` 改为 `enabled: true` 后跑全量（其余改动见 §5 旧版，
已随不翻转裁定回滚）：

- `Ran 1345 tests in 461.736s`，`FAILED (failures=55, errors=9, skipped=6)`。
- 失败清单（64 项，全文见 `~/workspace/github-maturity/bench-env/m31s2-fullsuite-flip-on.log`）：
  `test_adaptive_margin`、`test_agy`（3）、`test_calibration_gate`、
  `test_fleet`、`test_fleet_verify_evalid`、`test_guardrails`、
  `test_batch`（3）、`test_host_scope`（3）、`test_m25_slice1`（2）、
  `test_m25_slice2`（3）、`test_m27_slice2`（3）、`test_m28_slice1`（10）、
  `test_m29_slice1`（7）、`test_m30_slice1`（3）、`test_m30_slice2`（4）、
  `test_m31_slice1`（1）、`test_routing_weights`（1）、`test_weighted_routing`（1）
  等——全部是派单路径测试，共性为：用 shipped `POLICY` 默认值跑
  `run` / batch / fleet exec，本 VM codex 未登录致探针 unhealthy，
  门控拦下派单（`SystemExit: 2` / `ValueError: health gate blocked
  dispatch` / 卡片 `blocked`），与测试预期的派单成功冲突。
  其中 `test_m25_slice1/2` 与 `test_m31_slice1` 的 `gate_off_*` 测试
  是直接钉住“默认关闭”前提的；其余 60 余项是隐含依赖默认关闭的
  其他功能测试。
- 对照基线：切片 1 分支（默认关闭）全量 1341 例全绿；本次翻转分支
  仅多 4 例新测（`test_m31_slice2`），失败 55＋错误 9 均为翻转引入。
- 过程注记：第一跑（646s）因 `tail -8` 截断丢了汇总行，不作数；
  第二跑被 VM 换机 SIGTERM 杀死，不作数；以上为第三跑完整结果，
  日志落持久路径（`/tmp` 已在换机中清空，见 §6）。

## 5. 翻转评估与改动清单

裁定标准（#256 正文）：默认开启全量绿＋证据过关则翻为 `enabled: true`，
否则结论落单、不翻转。

| 依据 | 结论 |
| --- | --- |
| 跨主机证据（§2–§3）：deepseek 拦下＋codex 真实执行放行，账本行带 `probe_host` / `probe_side: executor` | 过关 |
| 默认开启全量套件（§4）：1345 例中 55 失败＋9 错误 | **不过关** |
| 显式关闭开关保留 | 过关（机制上）：`policy.local.json` 深合并可覆盖开关、`--no-health-gate` / `--skip-health-gate` 均有效，由 `tests/test_m31_slice2.py` 新测钉住；但开关存在不抵消套件红 |

**裁定：不翻转。** 证据过关只证明门控机制本身工作正常，不证明翻转
默认是安全的：64 项测试（约占套件 4.8%）隐含依赖默认关闭，翻转需要
一次测试套件迁移（逐项改为 mock 健康探针或显式关门控）才能全绿——
超出本切片范围，且逐项改测试会削弱 shipped 配置的覆盖。另有现实
推论：任何默认后端未登录的主机上，翻转后每次 `run` / batch / fleet
派单都会直接 exit 2（M24 设计如此，不改道），属于须单独拍板的大行为
变更，不宜以一行业务改动带过。

本 PR 内容（不翻转，故无翻转改动）：

- 本证据文档 `docs/m31-slice2-real-evidence.md`。
- `tests/test_m31_slice2.py`（新，4 项）：翻转机制可用
  （policy.local 式覆盖可开门控）、`--health-gate` 在默认关闭下可
  强制开、`--no-health-gate` / `--skip-health-gate` 在门控开启时仍
  可关/可绕过且记账。默认本身仍关闭，由 M24 测试钉住。
- 未动：`policy.json`（`enabled: false` 不变）、README、SKILL.md、
  `bin/openswarm`、CLI 名、bin 路径、`OPENSWARM_*` 环境变量、子命令
  签名、SKILL 契约；未做 swrm PyPI 占名；未删 `pi-worker-proxy`。

将来重提翻转的前提：① 64 项测试完成迁移且默认开启全量绿；
② 重新评估“未登录主机上全部派单 exit 2”的用户影响并由用户拍板。

## 6. 诚实注记（未达标 / 异常 / 局限）

- A1 echo 卡住（非门控问题，不计入放行完成证据）：远端 run
  `20261117-212525-741a2a` 的 `status.json` 为 `state queued`、
  `queue_reason: provider concurrency limit`、
  `fleet_reservation_error: fleet capacity coordinator unavailable:
  invalid backend/provider`。自定义 backend（echo）无 provider 映射，
  容量协调器无法预留、run 无限排队；控制端 300s 超时后远端进程
  （PID 59643）成孤儿，施工员已 kill，未留僵尸。此为既有局限，
  本切片不修（超出 #256 范围）；codex 有合法 provider，
  `queued_seconds 0.102` 即获槽，佐证卡住与 backend 有关、与门控无关。
- 误启动两起（已排除，不计入证据）：未设 `OPENSWARM_SSH_CONFIG` 前，
  两次 fleet exec 均报 transport rc=255（`kex_exchange_identification:
  read: Connection reset by peer`，`198.19.0.1:3128`）——裸 ssh 读空的
  `/root/.ssh/config` 致 gocc 别名无 ProxyJump。门控当时记
  unknown/allowed（`on_unknown=allow`）；账本已清空重跑，误启动行
  不在证据账本内。
- 翻转尝试已回滚：分支上曾把 `policy.json` 改为 `enabled: true` 并同步
  README / SKILL.md / 注释 / M24 测试，跑完 §4 套件后按不翻转裁定
  全部 `git checkout` 还原；本分支最终 diff 只有本证据文档与
  `tests/test_m31_slice2.py` 两文件。
- VM 换机：证据采集期间 VM 换机一次（uptime 归零约 13:52 SGT），`/tmp`
  被清空——控制端账本与 .out/.err 原文件丢失，本文档数字以换机前
  回读的逐字记录为准；控制端 home 已重建、套件第三跑进行中。
  （同次换机亦杀死了 evalid bench-m16s2 跑批，由主轮 / 日常轮处理，
  不属本切片。）
- codex 额度观察（非缺陷）：A2 之后 gocc codex 重测仍 healthy，但
  quota remaining 由 0.33 → 0.0（run 记录 `quota_before 0.67 →
  quota_after 1.0`）；quota 检查按“探针可读”判 pass，不按 remaining
  > 0 判。gocc 上另有 CX7/CX8/M20 三个 lane serve 进程在跑，不能把
  整个窗口消耗单归于本次调用。后续 codex 真实 run 须留意剩余额度。
- 远端 run 的账本/运行目录写在 gocc `~/.claude/openswarm`（默认 home），
  因 `fleet exec` 转发命令首 token 须为 `run`、无法前置 `env
  OPENSWARM_HOME=...` 做隔离；写入内容仅为本次两个 run
  （20261117-212525-741a2a 排队残留 + 20261117-213237-5602ef 完成），
  工作仓为新建的 `/tmp/swrm-m31s2-work`  scratch 仓，未动用户真实仓。
