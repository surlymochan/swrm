# swrm host scope 测试修复

REPORT: blocked — 已修复确认的 scope 测试兼容问题；全量测试受测试环境影响而中断，尚不能证明无新增失败。

## 已修复

- `tests/fixtures/fake_fleet_openswarm.py` 接受 `--scope repo|host`，默认 repo。此前 batch 无条件转发此参数，测试桩因未知参数退出。
- 更新 run、batch、lane assign 的 CLI help golden，包含新增 scope 参数及 argparse 换行。
- 新增实际使用 fake fleet 的 host scope 回归测试，验证 batch 成功且远端 run 收到 host scope。
- 未修改运行时代码，未提交、推送或切换分支。

## 实际验证

| 验证 | 结果 |
| --- | --- |
| `python3 -m pytest -q tests/test_batch.py tests/test_cli_golden.py` | 13 passed，6 subtests passed；28.25s |
| `git diff --check` | 通过 |
| a4ff878：`python3 -m pytest -q tests` | 中断时 81 failed、68 passed、6 subtests passed；276.79s，未完成 |
| 修复前 HEAD：同一全量命令 | 中断时 145 failed、52 passed、3 subtests passed；277.89s，未完成 |
| 修复后：同一全量命令 | 卡在首个测试的 provider admission；123.36s 后主动中断，未完成 |

基线通过 `git archive a4ff878` 解包至本 worktree 内临时目录运行，没有 checkout 或修改分支。以上中断计数覆盖的测试范围不同，不能直接相减判断回归。

## 已确认的既有问题（本次不修）

- `tests/test_guardrails.py::GuardrailsTests::test_enforced_risks_all_protocols_and_policy_opt_out`：a4ff878 与当前版本单独运行均因 finding 为 None 触发 TypeError。
- `tests/test_lane.py::LaneTests::test_abort_marks_codex_lane_run_stopped`：a4ff878 与当前版本单独运行均在等待 running 状态时超时（基线组合运行 2 failed，14.63s）。
- 全量运行两侧都有大量 `FileNotFoundError`：继承的 TMPDIR 指向本 worktree 的 `.openswarm-tmp`，测试运行期间该目录被 executor 清理，后续 tempfile 创建失败。这些失败不能作为 scope 回归证据。
- 测试的 `_load.py` 清理 OPENSWARM 环境变量后，仍会读取本机默认 fleet-capacity 配置；实际确认 `fleet_guard_command()` 非空。修复后的全量执行阻塞于 provider admission，基线也阻塞于相同代码。guardrails 固定 `NOW()`，该等待可能无法通过任务预算超时。

## 待完成

在独立、稳定的 TMPDIR 下运行，并为测试隔离本机 fleet-capacity 配置（仅测试，不调整生产容量或预留）；对 a4ff878 和修复后版本完成相同全量命令，逐项比较失败集合后再判 ready。本轮所有长时间测试已发送 SIGINT 停止，未改动本机 coordinator 配置。
