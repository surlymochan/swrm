# M9 slice 2: counterfactual evidence + default flip (#134)

> 施工员证据采集。只写实测数字；验收结论由主轮裁定。本切片先做只读对照评估，
> 再凭评估结果翻转 shipped 默认；翻转本身走 PR，不由本切片合并。

## 0. 结论先行

对照评估（真实 ledger，见 §1）：**两组、每组 n=8，改道 8/8（占比 1.00），
预期通过率 0.00 → 1.00，Δ +1.00**；`plan_failed` 0。报表数字与原始 ledger
手工重算逐项一致（§3）。**无回归信号**：全部 16 条改道建议的 quoted 通过率
（1.00）均高于实际 pick（0.00），无任何一条会改道去更差的后端。

据此翻转 shipped 默认：`policy.json` 的 `routing.weighted` 由 `false` 改为
`true`（§5）。显式关闭开关保留：`policy.local.json` 里写
`"routing": {"weighted": false}` 仍可关掉（回归测试
`test_weighted_off_*` 已改为显式 `weighted=False` 覆盖此路径）。

**诚实口径**：本 harness 是 M8 同款的退化对照（echo-bad 恒失败、echo-good
恒成功），Δ +1.00 是该 harness 的上限、不是对真实后端的通过率预测。
它证明的是：改道规则在真实 run 上按设计触发、16/16 次本应失败的 run
都会被救下；真实后端的收益须以后续真实 ledger 的复算为准。

## 1. 报表输出（实测）

命令（scratch 账本，见 §4）：

```console
OPENSWARM_HOME=<m9-slice2>/home openswarm report --weighted-eval
```

```text
weighted eval (ledger: /home/hatch/workspace/github-maturity/bench-env/m9-slice2/home/ledger.jsonl; margin: 0.10)
replay: a scored run would divert when the advised backend/model differs from the run's backend/model and the quoted advice pass rate beats the recorded actual-pick pass rate by at least the margin; expected = quoted advice rate for diverted runs, observed outcome otherwise; small-n = n < 8, expected delta indicative only
backend    card_type       n diverted  share  advised   actual expected  delta  flag
------------------------------------------------------------------------------------
echo-good  fix             8        8   1.00     1.00     0.00     1.00  +1.00
echo-good  test            8        8   1.00     1.00     0.00     1.00  +1.00
scored runs: 16 across 2 groups; diverted: 16; advice records: 16
```

`--json` 输出与上表一致（`total_scored: 16`、`total_diverted: 16`、
两组 `small_n: false`、`plan_failed: 0`；原始输出见
`bench-env/m9-slice2/logs/report-weighted-eval.json`）。

交叉核对 `report --advice-accuracy`（同一账本）：两组均为 n=0、n/a
（`scored runs: 0`、`advice records: 16`）——按其定义，只有实际跑在
被建议后端上的 run 才进入评分；本账本 16 条建议记录全部未跟随
（`not_followed` 16），故 n=0。这与加权评估的结论一致，不矛盾：
M8 账本提供“跟随样本”（校准），本账本提供“未跟随样本”（对照）。

## 2. 为什么 M8 账本单独不够：既有账本全是已改道样本

先对 M8 真实账本跑了同一报表（`bench-env/m8-slice2`，48 行）：

```text
echo-good  fix             8        0   0.00     1.00     1.00     1.00  +0.00
echo-good  test            8        0   0.00     1.00     1.00     1.00  +0.00
scored runs: 16 across 2 groups; diverted: 0; advice records: 16
```

M8 的 16 条可评分 run 全是在 `weighted: true` 下已改道跑在 echo-good
上的，报表重放时 advised == actual，改道 0、Δ 0.00——它证校准，不证
“不开加权会损失什么”。因此按任务要求新跑了默认配置下的对照实验（§4）：
建议照常记录、但不改道，16 次 run 真实跑在 echo-bad 上失败，
重放才量得出改道占比与预期收益。

## 3. 手工重算（与报表逐项核对）

重算方法：直接读原始 `ledger.jsonl`，逐行取 `routing_advice.pass_rate` /
`actual_pass_rate` 与 `exec_pass`，按切片 1 的改道规则手工判定（建议后端
≠ 实际后端 且 quoted − actual ≥ margin 0.10 即改道；改道 run 的预期结果
取 quoted，否则取实际结果），用普通算术求占比与通过率，不调用报表代码。

### (echo-good, fix)：n=8，改道 8/8；quoted 1.0×8，均值 1.00；实际 0/8，通过率 0.00；预期 1.00；Δ +1.00

| run id | 实际后端 | 建议后端 | quoted | actual_rate | exec_pass | 改道 |
|---|---|---|---|---|---|---|
| 20261111-070848-44661c | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070849-6a999e | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070849-7c24e1 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070850-de3a58 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070850-d61173 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070851-7b8788 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070851-60d66e | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070852-3b89d9 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |

### (echo-good, test)：n=8，改道 8/8；quoted 1.0×8，均值 1.00；实际 0/8，通过率 0.00；预期 1.00；Δ +1.00

| run id | 实际后端 | 建议后端 | quoted | actual_rate | exec_pass | 改道 |
|---|---|---|---|---|---|---|
| 20261111-070902-1eca84 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070903-68e5dd | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070904-4e2264 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070905-e247c8 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070906-baed34 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070907-3c94fe | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070907-08abb3 | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |
| 20261111-070908-18209b | echo-bad | echo-good | 1.0 | 0.0 | False | 是 |

### 核对结论

- 两组 n、改道数、占比、advised、actual、expected、delta 与 §1 报表逐项一致。
- 16 条记录的账本行均含 `weighted_routing: {applied: false,
  reason: "routing.weighted is false (record-only advice)"}`，且**无任何
  一行含 `routed_by_advice`**——默认配置下建议只记录、不改道，与
  `apply_weighted_routing` 的实现一致（见 §6）。
- 另有 32 行种子 run（每 card type 各 8 次显式 echo-bad 失败＋8 次显式
  echo-good 成功）无 `routing_advice` 记录（显式 `--backend` 时建议
  本不生成），不进入评分；账本共 48 行，全部对应一次真实
  `openswarm run` 调用。

## 4. 实验环境与做法（全部在 scratch 内，不动真实数据）

- Scratch 根目录：`~/workspace/github-maturity/bench-env/m9-slice2/`
  （由 `m8-slice2` 同结构复制，路径已全量替换；账本从空开始重建）。
  - `home/`：`OPENSWARM_HOME`（账本、runs、scratch policy 均在此，与
    `~/.claude/openswarm` 完全隔离）。
  - `home/policy.local.json`：
    `{"routing": {"advice_margin": 0.1, "default_order": ["echo-bad", "echo-good"], "min_samples": 8}}`
    ——**不含 `weighted` 键**，即默认配置；合并后的策略取 shipped
    `policy.json` 的 `routing.weighted: false`。
  - `home/ledger.jsonl`：本次全部账本行（48 行）。
  - `home/runs/`：每次 run 的 `result.json` / `task.md` / `status.json`。
  - `work/`：scratch git 仓（1 个初始提交），作每次 `run --cwd` 的目标目录。
  - `backends.json`：两个自定义后端（`--backends-file` 传入；沿用 M6/M8
    的 echo 脚本）：`echo-bad` 恒失败、`echo-good` 恒成功。
  - `run-swrm.sh`：环境封装（`OPENSWARM_HOME` +
    `OPENSWARM_STATE_FILE` 指向 bench-env/swrm 的测试夹具
    `tests/fixtures/state.json`，离线模式，不 probe 真实后端；
    执行的是 bench-env/swrm（main，含切片 1）的 `bin/openswarm`）。
  - `run-experiment.sh`：本次 48 次 run 的驱动脚本；`logs/`：每次命令的原始输出
    与报表输出。
- 状态文件：`OPENSWARM_STATE_FILE` 指向仓内测试夹具；未用任何用户凭据；
  未调用任何真实后端 CLI；未触 pi/额度。
- 每次 run 的通用参数：`--backends-file backends.json --cwd work --no-worktree
  --no-fallback --no-auto-review --budget-minutes 5 --type impl --difficulty 3`，
  prompt 均为 `Write a one-line greeting into echo-output.txt (m9 slice2 …)`。
  种子轮另加显式 `--backend`（显式后端优先于加权路由）；对照轮不带 `--backend`。
- 执行结果：48 次 run 退出码全部符合预期（bad 种子 rc=1、good 种子 rc=0、
  对照轮 rc=1）。对照轮每行的 `routing_advice` 均为 echo-good / pass_rate 1.0 /
  actual_pass_rate 0.0（echo-bad 8 次失败历史），与手工重算一致。
- 执行时间：2026-11-11 15:08–15:09 SGT（run id 戳为 UTC 07:08–07:09）。

## 5. 翻转改动（本 PR）

- `policy.json`：`routing.weighted` 由 `false` 改为 `true`（唯一行为改动）。
- `README.md`：加权路由段改述——shipped 默认现为 `true`，显式
  `routing.weighted: false` 为关闭开关；其余措辞不变。
- 无 `CHANGELOG.md`：swrm 仓内无此文件（发版说明走 GitHub Release，
  由主轮在发版时写），故不在本 PR 新建。
- 测试：`tests/test_batch.py` 与 `tests/test_fleet.py` 各一处
  `test_weighted_off_*` 原先依赖 shipped 默认 false，现改为显式
  `weighted=False`（关闭路径本身的覆盖不变）；`tests/test_weighted_routing.py`
  的 docstring 同步改述。另新增 `tests/test_weighted_eval.py` 的
  `test_real_ledger_all_diverted`，对本账本夹具断言 §3 手工重算的表。
- 未动的面：`apply_weighted_routing` 与三条派单路径的代码一行未改；
  显式 `--backend` 仍优先；CLI 名、bin 路径、`OPENSWARM_*` 变量、
  子命令签名、SKILL 契约均未动；未删 pi-worker-proxy；未做 PyPI 占名。

## 6. 验收项 ② 的顺序说明（重要）

总单 #131 验收 ② 要求“≥3 个真实 run 在默认配置（weighted 未显式设置）
下按建议改道且 ledger 有 `routed_by_advice`”。**在翻转前，这在字面上
无法达成**：`apply_weighted_routing` 在 `routing.weighted` 为 false 时
直接返回（`"routing.weighted is false (record-only advice)"`），默认配置
下永远不会产生 `routed_by_advice`——§4 的 16 次对照 run 与账本行即为
实证（0 行含该字段）。因此验收 ② 的验证 run 只能在翻转后的默认（true）
下跑：本 PR 分支代码＋不含 `weighted` 键的 policy.local.json，见
`bench-env/m9-slice2-verify/`（种子 32 次＋无 `--backend` 对照 16 次，
预期 rc=0 且 16 行均含 `routed_by_advice`）。该验证结果见 issue #134
的证据评论。

## 7. 本 PR 内容

- `policy.json`：`routing.weighted: false` → `true`。
- `README.md`：加权路由段改述 shipped 默认。
- `docs/m9-counterfactual-evidence.md`：本文件。
- `tests/fixtures/weighted-eval-real-ledger.jsonl`：§4 账本的逐字复制（48 行），
  供回归测试复算。
- `tests/test_weighted_eval.py`：新增 `test_real_ledger_all_diverted`，
  对该夹具断言 §3 手工重算的表（两组均为 n=8、改道 8/8、占比 1.00、
  advised 1.00 / actual 0.00 / expected 1.00 / Δ +1.00，总计 16/16）。
- `tests/test_batch.py`、`tests/test_fleet.py`、
  `tests/test_weighted_routing.py`：上述默认改述的测试同步。

## 8. 原始材料路径

- 证据文件（本文件）：`swrm/docs/m9-counterfactual-evidence.md`
- Scratch 环境与全部原始输出：`~/workspace/github-maturity/bench-env/m9-slice2/`
  （`home/ledger.jsonl`、`home/runs/<run id>/result.json`、`logs/`、
  `run-experiment.sh`、`backends.json`、`echo-good.py`、`echo-bad.py`、
  `home/policy.local.json`）
- 翻转后默认配置验证：`~/workspace/github-maturity/bench-env/m9-slice2-verify/`
- 报表实现（切片 1）：`swrm/bin/openswarm`（`compute_weighted_eval`，
  M9 #132，PR #133，`fdf2f111`）
- 验收原文：`surlymochan/swrm#131`（总单）、`#134`（本切片）
