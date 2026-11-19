# M36 slice 1: OTel span 建模＋导出核心（#280）

总单 #279（OTel tracing 导出）。本切片只做 span 建模＋导出核心＋测试；
真实 run 导出证据与总验收在切片 2。

## 命令

```sh
openswarm trace export <run-id> [--out <path>] [--endpoint <url>] [--no-file] [--timeout <sec>]
```

- 默认出口：本地 OTLP JSON 文件，`<run-dir>/trace.otlp.json`，无网络依赖。
- `--endpoint <url>`（或环境变量 `OPENSWARM_OTLP_ENDPOINT`）：
  OTLP/HTTP POST，`Content-Type: application/json`，body 为标准
  `ExportTraceServiceRequest`（`resourceSpans`）。只有 2xx 才算发送成功；
  非 2xx / 连接失败时退出码 1、stderr 明确报错，绝不谎称已发送。
  `--endpoint` 与本地文件可并用（文件是本地留底，不是发送成功的证据）。
- `--no-file` 须与 `--endpoint` 并用；否则退出码 2。
- 缺 run（无 run 目录）或缺 `result.json` 时退出码 2。
- 成功时 stdout 打印 JSON 摘要：`run_id / trace_id / spans / file /
  endpoint / http_status / exported`。`exported: true` 只在请求的出口
  全部成功后打印。

实现：`bin/openswarm` 内 `build_run_trace()` / `post_otlp()` /
`cmd_trace_export()`，stdlib only（`json / hashlib / urllib`），零新依赖；
不改既有子命令签名与 SKILL 契约，不改派单/重试/门禁/路由语义。

## Span 树

```
swrm.run                          根 span；status.json started..finished
├── swrm.dispatch                 路由＋探活＋排队（started..execution_started）
├── swrm.execute                  首轮执行窗口；父 span 为各 attempt span
│   ├── swrm.attempt (×N)        result.json attempts[] 逐条，顺序串联
├── swrm.rework                   仅当 auto_rework.attempts > 0；覆盖末 N 条
│                                 attempt span，并以 links 指向它们
└── swrm.verify                   仅当 result.json 有 verify 字典；终于根 span 结束
```

## 映射规则（来源 → span）

| 来源 | 映射 |
| --- | --- |
| run id | `trace_id = sha256("openswarm-trace:" + run_id)[:32]`（十六进制）；span id = `sha256(trace_id + ":" + span名 + ":" + 序号)[:16]`。同一 run 每次导出同一 trace，可复核 |
| `status.json` started / finished | 根 span 起止（纳秒，OTLP JSON 里为字符串） |
| `status.json` execution_started / queued_seconds | dispatch span 止点（无 execution_started 时用 started + queued_seconds，夹在根 span 内） |
| `result.json` attempts[]（顺序） | 每个 attempt 一个 span；绝对开始时间未记录，故按顺序串联：上一个结束＝下一个开始，首个始于 dispatch 止点；`seconds` 为时长；超出根 span 结束时截断到根 span 结束 |
| attempt `meta.usage` / `events` 文件 | 属性 `swrm.usage.*`（input/output/cacheRead/cacheWrite）；事件文件只计行数与按 `type` 计的分类数（`swrm.events.lines / types`），**不读事件正文、不把任务文本/标题带入 span** |
| attempt `rc` / `error_class` | attempt span 的 OTel status：rc ≠ 0 或有 error_class → ERROR（message 为 error_class），否则 OK |
| `auto_rework.attempts = N` | rework span 覆盖末 N 条 attempt span（每次 rework 派单至少产生一条 attempt，故尾部 N 条必为 rework）；属性 `swrm.rework.attempts / max_attempts / recovered / final_verify_rc`，links 指向这 N 条 attempt span |
| `result.json` verify `{phase, rc, seconds, failing}` | verify span：止于根 span 结束，始于 `max(末 attempt 结束, 结束 − seconds)`；`swrm.verify.rc / phase / seconds / failing_count`；rc ≠ 0 → ERROR |
| `result.json` verify_evalid.verdict | 根 span 属性 `swrm.verify_evalid.verdict` |
| `result.json` status / error_class | 根 span status：有 error_class，或 status 为 failed/stopped，或 verify rc ≠ 0 → ERROR，否则 OK |
| ledger 行 | 扫描 `$OPENSWARM_HOME/ledger.jsonl` 取末一条匹配行：根 span 属性 `swrm.ledger.line`（1 起行号）、`swrm.ledger.offset`（字节偏移）、`swrm.ledger.found`；无匹配行时只记 `found=false`，不报错 |
| `result.json` 其余结构化字段 | 根 span 属性：`swrm.backend / model / effort / card_type / rework_of / commit / branch / run.seconds / usage.input_tokens / usage.output_tokens` |

时间回退链（按序取第一个可用值）：started 取 `status.json` started，
缺失时取 `result.json` 文件 mtime − wall seconds；finished 缺失时取
started + wall seconds。所有 span 起止均夹在根 span 内（`end ≥ start`），
保证时间单调。

## 导出样本

`tests/fixtures/trace-sample.otlp.json`：对 fixture run
`20261119-090000-m36s1x`（1 条失败 attempt + 1 条 rework attempt 已恢复 +
verify rc 0，7 个 span）的完整导出；测试
`tests/test_m36_slice1.py::test_fixture_sample_matches_committed_file`
断言 builder 输出与该文件逐字节等价（除 JSON 缩进外）。

## 护栏测试

`tests/test_m36_slice1.py`（14 例）：映射逐项断言、父子关系、时间单调与
串联时刻、ledger 行指针与文件实位一致、缺 run / 缺 result.json /
缺 events 文件 / 无 verify 无 rework 的最小 run、文件导出默认路径与
显式 `--out`、本地 HTTP 端点 202 成功与请求体、不可达端点报错且不
谎称成功、`--no-file` 缺 endpoint、CLI 端到端（子进程跑真实
`bin/openswarm trace export`）。
