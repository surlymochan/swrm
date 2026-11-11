# M16 slice 1: gate-flip evaluation evidence (#164)

> 施工员证据采集。只写实测数字；翻转裁定由切片 2（真实 run＋证据过关才翻）做，
> 本切片只做只读对照评估机制，不改派单、不改 shipped 默认、不改 ledger。

## 0. 结论先行

对照评估（真实 ledger，见 §1）：两处 M15 切片 2 证据账本共 48 条评分 run，
只会改道 1 条、拦截 1 条（改道占比 1.00、占评分 run 0.03）；被拦截 run 的
实际通过率 0.00 vs 跟随建议的预期 0.50（建议高估 +0.50）；**校准 Δ +0.00**
（0.641026 → 0.641026）、quoted-basis Δ −0.01282。拦截 run ID
`20261111-231536-ca5ae9` 与账本里记录的 `gated_by_calibration` 记录逐条一致，
重放复现了 live 门控真实做出的那次拦截。

**诚实口径**：这份证据 alone 不支持翻转默认——校准 Δ 为 0.00 且拦截样本只有
1 条：被拦截那次，门控保住的默认 pick（echo-bad）当时观测通过率也是 0.00，
与被建议组当时的观测通过率（0.00）相同，拦与不拦在校准口径下无差别。
翻转与否须等切片 2 的真实 run（默认配置下 ≥3 个 run 按门控决策执行）攒出
更多拦截样本后再裁定。本切片交付的是评估机制本身（§2 方法＋ §3 测试），
不是翻转结论。

## 1. 报表输出（实测，M15 切片 2 证据账本，只读读取）

命令（账本为 M15 切片 2 证据，原文件未改动）：

```console
OPENSWARM_HOME=<m15s2>/evidence/home-a openswarm report --gate-eval
OPENSWARM_HOME=<m15s2>/evidence/home-b openswarm report --gate-eval
```

home-a（72 行账本，39 条评分 run）：

```text
gate eval (ledger: .../evidence/home-a/ledger.jsonl; margin: 0.10; gate: diff > 0.20 and n >= 8)
backend    card_type       n diverted intercepted  share int-actual int-quoted cal-delta  flag
----------------------------------------------------------------------------------------------
echo-flaky fix            34        1           1   1.00       0.00       0.50     +0.00
echo-flaky test            5        0           0    n/a        n/a        n/a     +0.00  small-n
scored runs: 39 across 2 groups; would divert: 1; intercepted: 1 (1.00 of diversions, 0.03 of scored); advice records: 39
expected pass rate (quoted basis): gate off 0.65 vs gate on 0.64 (delta -0.01)
expected pass rate (calibrated): gate off 0.64 vs gate on 0.64 (delta +0.00)
intercepted runs: actual 0.00 vs expected-if-followed 0.50 (over-promise +0.50 over 1 runs)
```

home-b（25 行账本，9 条评分 run）：

```text
gate eval (ledger: .../evidence/home-b/ledger.jsonl; margin: 0.10; gate: diff > 0.20 and n >= 8)
backend    card_type       n diverted intercepted  share int-actual int-quoted cal-delta  flag
----------------------------------------------------------------------------------------------
echo-good  test            9        0           0    n/a        n/a        n/a     +0.00
scored runs: 9 across 1 groups; would divert: 0; intercepted: 0 (n/a of diversions, 0.00 of scored); advice records: 9
expected pass rate (quoted basis): gate off 1.00 vs gate on 1.00 (delta +0.00)
expected pass rate (calibrated): gate off 1.00 vs gate on 1.00 (delta +0.00)
intercepted runs: none (no diversion would be blocked)
```

`--json` 输出与上表一致（home-a：`total_scored: 39`、`total_would_divert: 1`、
`total_intercepted: 1`、`intercepted_ids: ["20261111-231536-ca5ae9"]`、
`expected_delta: -0.01282`、`calibrated_delta: 0.0`；home-b 全 1.00、拦截 0）。

## 2. 方法：为什么重放必须按时间前缀

全量账本终态下，echo-flaky/fix 组看起来是校准的（`report --advice-accuracy`：
advised 0.70062 vs actual 0.757576，diff 0.057，n=33，不失准）——若拿终态表
重放，拦截数会是 0，live 门控真实做出的那次拦截就被“洗掉”了。`report
--gate-eval` 因此按账本顺序走、每条评分 run 用它之前的严格前缀算校准表：
被拦截 run 派发时，前缀表为 advised 0.725372 vs actual 0.00、diff 0.725372、
n=8，正是账本里该 run 的 `weighted_routing` 记录所载的门控数值
（`calibration_diff: 0.725372`、`calibration_samples: 8`），逐项一致。
同一组后来跑好了（diff 回落到 0.057），前缀重放只拦当时的那 1 条——与 live
门控“每决策按当时账本重估、校准恢复即恢复跟随”的行为一致。

两个 Δ 的分工（见 README）：quoted-basis 按 M9 口径把建议原样计入（被拦改道
按 quoted 0.50 计），所以它对“拦住一句谎言”收了 −0.01282 的账面价；
calibrated-basis 把跟随建议的预期换成被建议组当时的观测通过率（0.00），
拦与不拦同为 0.00，Δ +0.00——翻转裁定只看后者。拦截样本 n=1 时，两个 Δ
都只是单样本陈述，不是总体结论。

## 3. 手工重算（home-a fix 组）

- 评分 run 34 条：观测通过 25 条（实际通过率 25/34 = 0.735294）。
- 唯一会改道的是被拦截 run（quoted 0.50 vs 实际 pick 0.00，优势 0.50 ≥
  margin 0.10；该 run 实际失败，观测 0）。
- quoted-basis 门控关：(25 + 0.50)/34 = 0.75；门控开：(25 + 0.00)/34 =
  0.735294；Δ −0.014706。
- calibrated-basis 门控关：被拦 run 按前缀组观测率 0.00 计，(25 + 0.00)/34 =
  0.735294；门控开：按默认 pick 记录率 0.00 计，同为 0.735294；Δ 0.00。
- 全账本：quoted 关 (25.5)/39 = 0.653846、开 25/39 = 0.641026、Δ −0.01282；
  calibrated 关/开均为 25/39 = 0.641026、Δ 0.00。test 组 5 条全失败、无改道，
  不影响差值。以上数字与 `--json` 输出逐项一致。

## 4. 合成账本边界（tests/test_gate_eval.py，手工推导值见文件头注释）

- 失准组（quoted 1.00 vs 实际 0.25，n=8）：4 条改道全被拦截，校准 Δ
  +0.083333（拦截把预期从 0.25 拉到 0.333333），quoted Δ −0.133334。
- 校准组（diff 0.025）：4 条改道 0 拦截，两 Δ 均为 0。
- 失准但 n=4（diff 1.00）：样本不足，0 拦截。
- 边界 diff 恰好 0.20：严格大于才失准，0 拦截。
- 校准恢复组：失准期 2 条改道被拦截，24 条好 run 把 diff 拉回 0.1125 后，
  后 2 条改道不再拦截；校准 Δ +0.013889。
- 自定义门控栏：diff 栏放宽到 0.80 则失准组不再拦截；样本栏降到 4 则
  n=4 的失准组被拦截。margin 0.60 时全账本 0 改道，两预期率坍缩为观测率
  53/82 = 0.646341。
- 空账本：0 组、0 评分、“no advice history”，不崩溃。

## 5. 测试结果

一次合并运行（`python3 -m unittest discover -s tests -p <suite>`，8 个套件）：

| 套件 | 通过/总数 |
| --- | --- |
| test_gate_eval（新增） | 20/20 |
| test_weighted_eval（M9 邻近） | 15/15 |
| test_calibration_gate（M15 邻近） | 12/12 |
| test_advice_accuracy（M8 邻近） | 10/10 |
| test_cli_golden（金测） | 2/2 |
| test_routing_weights（邻近） | 7/7 |
| test_route（邻近） | 25/25 |
| test_fleet_verify_evalid（邻近） | 26/26 |
| **合计** | **117/117** |

新测试覆盖：合成账本边界（无失准组、失准组 n<8、失准组 n≥8 且差值>0.20、
边界差值恰好 0.20、校准恢复后组、缺 quoted 率、无评分 run、空账本）、
自定义门控栏、非法参数、确定性；真实账本重放（home-a 拦截 1 条且 ID 与
账本记录一致、home-b 拦截 0 条）；文本金测；命令文本/JSON/缺账本/经
`cmd_report` 派发/CLI 子进程。

## 6. 范围与未做事项

- 本切片只做评估机制（`report --gate-eval`＋测试＋文档）；不改派单、不改
  `policy.json` shipped 默认（`routing.calibration_gate` 仍为关闭）、不改
  ledger、不碰子命令签名与 SKILL 契约。
- 重放用静态 margin（policy 的 `routing.advice_margin`，默认 0.10），与 M9
  相同；`routing.adaptive_margin` 开启时的自适应 margin 不参与重放。
- 前缀表按账本文件顺序（追加顺序＝派发顺序）计算；账本若被重排，重放结果
  会变——这是方法本身的属性，不是 bug，见 `compute_gate_eval` 文档串。
- 翻转裁定留给切片 2：须先在默认配置下跑 ≥3 个真实 run，再凭校准 Δ 与
  回归信号裁定；本证据的校准 Δ +0.00（n=1）不构成翻转依据。
