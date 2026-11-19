# M37 slice 1: MCP server 骨架＋dispatch/query 工具 (#285)

`openswarm mcp` 启动一个 MCP server（stdio transport，JSON-RPC 2.0，每行一帧）。
协议版本 **2025-06-18**（`initialize` 响应写明该版本；客户端报其他版本时，
server 仍以 2025-06-18 应答）。

## 启动

```sh
openswarm mcp
```

server 只在该显式命令下启动，不改默认行为、不常驻、无监听端口、无新依赖
（stdlib `json` over stdio）。其他子命令签名与 SKILL 契约不动。

## 方法

| 方法 | 说明 |
| --- | --- |
| `initialize` | 需 `params.protocolVersion`（字符串）；返回本 server 的协议版本、capabilities `{tools: {}}`、serverInfo `{name: "openswarm", version}` |
| `ping` | 返回 `{}` |
| `tools/list` | 返回 `dispatch`、`query` 两个工具（含 inputSchema） |
| `tools/call` | 按名调用；未知工具／非法参数返回 JSON-RPC 错误 `-32602` 并列出可用工具与合法参数，不静默失败 |
| `notifications/*` | 无响应（如 `notifications/initialized`） |

其他方法返回 `-32601`，非法 JSON 行返回 `-32700`，非法请求对象返回 `-32600`；
handler 内部异常返回 `-32603` 且 server 继续服务。

## 工具

### dispatch

把一张任务卡片走现有 `run` 路径：派单 → 执行 → verify → ledger 全程留痕。
实现上以子进程跑 `openswarm run`（stdio 全捕获），即字面意义上的同一条
run 路径；子进程的 stdout 永远不会污染 MCP 流。dispatch 会阻塞到 run 结束。

参数：`task`（必填，非空字符串）、`title`、`backend`、`verify`（验收命令）、
`budget_minutes`（> 0）、`dry_run`（只路由不执行，默认 false）、
`skip_health_gate`（绕过派单前健康门，与 CLI 同名 flag 一致、默认 false；
绕过会记进 ledger 与 result.json）。

成功返回 run 的结构化记录（含 `id`、`decision`、`status` 等）；
失败返回 `isError: true` 的结果，内含子进程 exit 码、stderr 尾部与复现提示，
且子进程已打印的结构化失败记录（如健康门拦截及其证据）原样附在 `run` 字段里。

### query

按 run id 查状态／ledger 摘要。返回 `run_id`、`state`、`status`、`title`、
`backend`、`model`、`commit`、`elapsed_min`、`budget_min`、`verify`（rc 与
failing 列表）、`verify_evalid`、`error`／`error_class`、`ledger_found` 与
ledger 条目、`run_dir`。未知 run id 返回 `isError: true` 并提示用
`openswarm runs -n 20 --json` 列真实 id（不含斜杠的 id 校验同理）。

## 最小客户端验证（本片）

`/tmp/mcp_client.py`（未入库的证据脚本）对 server 做 initialize → tools/list →
dispatch（dry_run，skip_health_gate）→ query → 未知工具 → 缺 task → 非法 JSON：

- initialize：`protocolVersion 2025-06-18`，`serverInfo {name: openswarm, version: 0.4.55}`
- tools/list：`[dispatch, query]`
- dispatch：run `20261119-071719-93b160`，`dry_run: true`，decision backend `codex`／model `gpt-6-luna`
- query：同一 run id，`state: dry-run`
- 未知工具：`-32602 unknown tool 'bogus'; available tools are: dispatch, query`
- 缺 task：`-32602 tool 'dispatch' requires argument 'task'`
- 非法 JSON：`-32700`；server 收到 EOF 后 exit 0

真实派单端到端（非 dry-run）证据归切片 2（#284 验收③）。

## 边界

- 只做 stdio，不做 HTTP/SSE 远端形态（#284 范围外）。
- 不改派单／重试／路由语义；不改健康门默认（门默认仍开，拦截时 dispatch 如实返回失败记录）。
- `dispatch` 不代填真人承诺：任务卡片内容原样透传给 `run`。
