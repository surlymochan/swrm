REPORT: ready — host 告警不再遮蔽强制风险，报告显示 scope；同环境完整测试对比无新增失败（两侧均有 3 项既有错误）。

# host scope 评审修复验证

验证日期：2026-11-08。环境：macOS，Python 3.14.3。
当前基于 `fed32ac1876bec1e8e398445b9630b930ee89146` 加本次未提交修复；基线为 `a4ff878`。
未提交、推送或切换分支。

## 评审项与证据

| 评审项 | 修复与验证 |
| --- | --- |
| BLOCKER：sudo 提前返回，遮蔽后续强制项 | 暂存 host 告警，继续扫描后续 shell 片段、嵌套 shell 和事件中的其他命令；发现强制风险立即返回阻断。`test_host_warnings_do_not_hide_enforced_risks` 覆盖 sudo/外部 rm 与 push、publish、secret print 的组合及顺序。 |
| MAJOR：报告缺少 scope | 普通 report 的活动/已完成任务行显示 scope；每日 JSON 和 HTML 的最慢/失败任务行记录并显示 scope，旧 ledger 默认 repo。`test_compact_report_displays_active_and_finished_scopes`、每日 JSON/HTML 测试通过。 |
| MAJOR：全量对比证据缺失 | 两侧均完整运行 unittest discovery，统计和失败集合见下表。基线只覆盖测试隔离 helper，没有修改业务代码或测试断言。 |

默认 repo 提示及风险行为保持不变；`test_host_scope_prompt_and_risk_softening` 通过。
README.md、SKILL.md 补充混合命令仍阻断的说明及报告展示约定。

## 聚焦与运行链路验证

以下 5 项通过：

```sh
TMPDIR="$PWD/.tmp/test-tmp" PYTHONPATH=tests python3 -m unittest \
  test_guardrails.GuardrailsTests.test_host_scope_prompt_and_risk_softening \
  test_guardrails.GuardrailsTests.test_host_warnings_do_not_hide_enforced_risks \
  test_guardrails.GuardrailsTests.test_compact_report_displays_active_and_finished_scopes \
  test_guardrails.GuardrailsTests.test_daily_report_counts_backend_lane_usage_and_risks \
  test_guardrails.GuardrailsTests.test_daily_cli_html_and_json_are_offline_and_escape_summaries -v
```

将 HEAD 中的风险/报告函数加载回相同模块后，新回归断言能复现 sudo 遮蔽风险、普通报告缺 scope、每日 JSON 缺 scope；修复后以上 5 项均通过。

另以临时 git fixture、host status.json 和仅打印 JSON 事件的 Python 假执行器，实测 `run_proc`：

| 事件中的命令文本（未实际执行） | 返回码 | 记录 |
| --- | --- | --- |
| `sudo true` | 0 | risk_warning / sudo |
| `rm -rf /outside/private` | 0 | risk_warning / rm outside worktree |
| `sudo true; git push origin main` | 133 | risk_blocks / push |
| `sudo true; cat .env` | 133 | risk_blocks / secret print |
| `sudo true; npm publish` | 133 | risk_blocks / publish |

运行证据位于本 worktree `.tmp/host-risk-runtime.json`，复现脚本为 `.tmp/check-host-risk-runtime.py`。
HTML 的 scope 列、host/repo 值及转义通过现有 CLI HTML 测试验证；未做浏览器截图。

## 完整测试对比

两侧使用同一 Python、同一 worktree 内 TMPDIR（`.tmp/test-tmp`），独立 OPENSWARM_HOME。
使用 `git archive a4ff878` 在 `.tmp/baseline` 解包，不切分支；仅将当前 `tests/_load.py` 复制到基线，
确保两侧都隔离 fleet guard 配置与 capacity 路径，避免访问真实控制面。
这是加相同测试隔离 helper 的基线结果，并非未修改 helper 的原始基线运行。

两侧调用相同 runner；runner 使用 `unittest.defaultTestLoader.discover('tests')` 和
`unittest.TextTestRunner(verbosity=2)`，无筛选、无提前停止、无套件超时，并记录所有失败 ID。
等价全量入口为在各自根目录运行：

```sh
TMPDIR="$PWD/.tmp/test-tmp" python3 -m unittest discover -s tests -v
```

实际运行（两个进程各自 chdir 到对应源码根目录）：

```sh
TMPDIR="$PWD/.tmp/test-tmp" python3 .tmp/run-suite.py "$PWD" "$PWD/.tmp/current-suite"
TMPDIR="$PWD/.tmp/test-tmp" python3 .tmp/run-suite.py "$PWD/.tmp/baseline" "$PWD/.tmp/baseline-suite"
```

| 版本 | 测试数 | 通过 | failures | errors | skips | 耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| a4ff878 + 相同隔离 helper | 409 | 403 | 0 | 3 | 3 | 312.89s |
| 当前修复 | 413 | 407 | 0 | 3 | 3 | 312.22s |

失败集合完全相同：新增 `[]`，消失 `[]`。两侧均非全绿，不能将零新增失败表述为全量通过。

| 两侧共同错误 | 现象 |
| --- | --- |
| `test_batch.BatchTests.test_detached_cli_runs_entire_batch_in_background` | subprocess 超过测试既有 10 秒超时 |
| `test_guardrails.GuardrailsTests.test_enforced_risks_all_protocols_and_policy_opt_out` | 读取 None 风险结果时 TypeError |
| `test_guardrails.GuardrailsTests.test_rm_block_records_only_path_class` | 读取 None 风险结果时 TypeError |

两侧共同跳过：Linux bubblewrap/socat 隔离测试 2 项；非 macOS Keychain 行为测试 1 项。
本次未扩大范围修复这些基线错误。

完整逐项日志及机器可读结果保留在本 worktree（忽略目录，不纳入提交）：

- `.tmp/baseline-suite.log`、`.tmp/baseline-suite.json`
- `.tmp/current-suite.log`、`.tmp/current-suite.json`
- `.tmp/review-red.log`、`.tmp/guardrails.log`

`git diff --check` 通过。测试期间未操作真实 fleet 控制面或执行风险命令。
