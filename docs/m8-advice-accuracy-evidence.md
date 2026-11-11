# M8 slice 2: real-ledger advice accuracy evidence (#129)

> 施工员证据采集。只写实测数字；验收结论由主轮裁定。本切片只做只读报表与证据，
> 未改任何派单行为；shipped `policy.json` 的 `routing.weighted` 仍为 `false`。

## 0. 结论先行

`report --advice-accuracy` 在真实 ledger 上跑出 **2 组、每组 n=8**，两组建议通过率与
实际通过率均为 1.00，差值均为 0.00。**无失准组**（失准规则：差值 >0.20 且 n≥8；
两组差值分布：0.00、0.00）。报表数字与原始 ledger 手工重算一致（§3）。

## 1. 报表输出（实测）

命令（scratch 账本，见 §4）：

```console
OPENSWARM_HOME=<m8-slice2>/home openswarm report --advice-accuracy
```

```text
advice accuracy (ledger: /home/hatch/workspace/github-maturity/bench-env/m8-slice2/home/ledger.jsonl)
advised = mean pass rate quoted on each scored run's routing_advice; actual = observed pass rate of runs that ran on the advised backend; diff = |advised - actual|; MISCALIBRATED when diff > 0.20 and n >= 8
backend    card_type     advised   actual    n   diff  flag
----------------------------------------------------------------
echo-good  fix              1.00     1.00    8   0.00
echo-good  test             1.00     1.00    8   0.00
scored runs: 16 across 2 groups; advice records: 16
```

`--json` 输出与上表一致（`total_scored: 16`、`total_advice_records: 16`、
两组 `miscalibrated: false`、`not_followed: 0`；原始输出见
`bench-env/m8-slice2/logs/report-advice-accuracy.json`）。

## 2. 为什么要新跑真实 run：既有账本样本不足

先清点了 M6/M7 既有真实账本的可评分样本（可评分 = `routing_advice.backend`
非空、且 run 实际跑在该后端、有已知结果）：

| 账本 | 总行数 | 可评分样本 |
|---|---|---|
| `bench-env/m6-slice2/home/ledger.jsonl` | 18 | 0（仅 2 行带 `routing_advice`，且 `backend: null`） |
| `bench-env/m6-slice2-rerun/home/ledger.jsonl` | 19 | 3，均为 (echo-good, fix)，quoted 1.0、exec_pass True：`20261110-210644-9a25df`、`20261110-210644-7cf182`、`20261110-215054-9f1e5e` |
| `bench-env/m7-slice2/home/ledger.jsonl` | 24 | 0（加权决策只落在 batch/fleet 决策记录行，报表按规则排除） |

三账本合计仅 3 条可评分样本，不满足「≥2 组、每组 n≥8」。因此按任务要求新跑真实
加权派单（§4），未手工编造任何账本行。

## 3. 手工重算（与报表逐项核对）

重算方法：直接读原始 `ledger.jsonl`，逐行取 `routing_advice.pass_rate` 与结果
（`accepted` 为 bool 时优先，否则 `exec_pass`；与 `routing_outcome()` 同口径），
用普通算术求均值与通过率，不调用报表代码。行号为 0-based。

### (echo-good, fix)：n=8，quoted 1.0×8，均值 1.00；结果 8/8，通过率 1.00；差值 0.00

| 账本行 | run id | quoted pass_rate | exec_pass |
|---:|---|---|---|
| 16 | 20261111-041458-77031c | 1.0 | True |
| 17 | 20261111-041459-511065 | 1.0 | True |
| 18 | 20261111-041459-74f395 | 1.0 | True |
| 19 | 20261111-041500-4e1dc4 | 1.0 | True |
| 20 | 20261111-041500-733b9a | 1.0 | True |
| 21 | 20261111-041501-3d4d79 | 1.0 | True |
| 22 | 20261111-041501-491da7 | 1.0 | True |
| 23 | 20261111-041502-44e0bc | 1.0 | True |

### (echo-good, test)：n=8，quoted 1.0×8，均值 1.00；结果 8/8，通过率 1.00；差值 0.00

| 账本行 | run id | quoted pass_rate | exec_pass |
|---:|---|---|---|
| 40 | 20261111-041510-f96351 | 1.0 | True |
| 41 | 20261111-041510-ec9379 | 1.0 | True |
| 42 | 20261111-041511-06578f | 1.0 | True |
| 43 | 20261111-041511-f78ea1 | 1.0 | True |
| 44 | 20261111-041512-82ed7d | 1.0 | True |
| 45 | 20261111-041512-cd68db | 1.0 | True |
| 46 | 20261111-041513-a99186 | 1.0 | True |
| 47 | 20261111-041513-ec72b9 | 1.0 | True |

### 核对结论

- 两组 n、advised、actual、diff 与 §1 报表逐项一致；`not_followed` 均为 0
  （16 条建议记录全部跑在被建议后端，无未跟随样本）。
- 差值分布：两组 diff 均为 0.00；无任何组满足差值 >0.20 且 n≥8，故**无失准组**。
- 另有 32 行种子 run（每 card type 各 8 次显式 echo-bad 失败＋8 次显式 echo-good
  成功）无 `routing_advice` 记录，不进入评分；账本共 48 行，全部对应一次真实
  `openswarm run` 调用（dry-run 不写账本）。

## 4. 实验环境与做法（全部在 scratch 内，不动真实数据）

- Scratch 根目录：`~/workspace/github-maturity/bench-env/m8-slice2/`
  （由 `m6-slice2-rerun` 同结构复制，路径已全量替换；账本从空开始重建）。
  - `home/`：`OPENSWARM_HOME`（账本、runs、scratch policy 均在此，与
    `~/.claude/openswarm` 完全隔离）。
  - `home/policy.local.json`：
    `{"routing": {"advice_margin": 0.1, "default_order": ["echo-bad", "echo-good"], "min_samples": 8, "weighted": true}}`
    ——仅 scratch policy 开 `weighted: true`；shipped 默认不动。
  - `home/ledger.jsonl`：本次全部账本行（48 行）。
  - `home/runs/`：每次 run 的 `result.json` / `task.md` / `status.json`。
  - `work/`：scratch git 仓（1 个初始提交），作每次 `run --cwd` 的目标目录。
  - `backends.json`：两个自定义后端（`--backends-file` 传入；沿用 M6 证据的
    echo 脚本，路径改到本目录）：`echo-bad` 恒失败、`echo-good` 恒成功。
  - `run-swrm.sh`：环境封装（`OPENSWARM_HOME` +
    `OPENSWARM_STATE_FILE` 指向仓内测试夹具 `swrm/tests/fixtures/state.json`，
    离线模式，不 probe 真实后端）。
  - `run-experiment.sh`：本次 48 次 run 的驱动脚本；`logs/`：每次命令的原始输出
    与报表输出。
- 状态文件：`OPENSWARM_STATE_FILE` 指向仓内测试夹具；未用任何用户凭据；
  未调用任何真实后端 CLI。
- 每次 run 的通用参数：`--backends-file backends.json --cwd work --no-worktree
  --no-fallback --no-auto-review --budget-minutes 5 --type impl --difficulty 3`，
  prompt 均为 `Write a one-line greeting into echo-output.txt (m8 slice2 …)`。
  种子轮另加显式 `--backend`（显式后端优先于加权路由）；加权轮不带 `--backend`。
- 执行结果：48 次 run 退出码全部符合预期（bad 种子 rc=1、good 种子 rc=0、
  加权轮 rc=0）。每轮加权 run 的账本行均含 `routed_by_advice: true` 与
  `weighted_routing: {applied: true, from: echo-bad, to: echo-good, advantage: 1.0,
  margin: 0.1}`（例：`20261111-041458-77031c` 的 `result.json` 与账本行一致）。
  建议样本数随入账成功行自然递进（fix 组 8→15、test 组 8→15），quoted
  pass_rate 始终 1.0。

## 5. 本 PR 内容

- `tests/fixtures/advice-accuracy-real-ledger.jsonl`：§4 账本的逐字复制（48 行），
  供回归测试复算。
- `tests/test_advice_accuracy.py`：新增 `test_real_ledger_two_groups_n_eight`，
  对该夹具断言 §3 手工重算的表（两组均为 1.00 / 1.00 / n=8 / diff 0.00，
  无失准组，总计 16/16）。
- 本文件：证据全文。

## 6. 原始材料路径

- 证据文件（本文件）：`swrm/docs/m8-advice-accuracy-evidence.md`
- Scratch 环境与全部原始输出：`~/workspace/github-maturity/bench-env/m8-slice2/`
  （`home/ledger.jsonl`、`home/runs/<run id>/result.json`、`logs/`、
  `run-experiment.sh`、`backends.json`、`echo-good.py`、`echo-bad.py`、
  `home/policy.local.json`）
- 报表实现（切片 1）：`swrm/bin/openswarm`（`compute_advice_accuracy`，
  M8 #127，PR #128，`5840622f`）
- 验收原文：`surlymochan/swrm#126`（总单）、`#129`（本切片）
