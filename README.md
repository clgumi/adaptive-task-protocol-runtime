# Adaptive Task Protocol Runtime

一个面向 Hermes 的外置、非 LLM 任务协议 Runtime 和 Bridge。

它把复杂任务从“模型说做完了”变成可恢复、可验证、可审计的流程：

```text
Task Contract
  -> Plan / Todo
  -> 原 Hermes Profile 执行
  -> Self-Check
  -> Build / Test / Readiness
  -> Evidence
  -> Deterministic Gate
  -> CLOSED
```

## 这个工具解决什么问题

普通对话适合即时问答和轻量操作，但复杂项目通常会遇到：

- 任务执行到一半后，模型忘记目标、范围或下一步；
- 多个 Todo 的完成状态只存在于自然语言中，无法可靠恢复；
- “测试通过”“已经部署”等结论没有对应的命令结果和文件证据；
- 验证完成后又修改了代码，旧的 PASS 仍被错误使用；
- 任务失败后只能重新开始，无法区分 Planner 失败、执行失败和证据缺失；
- 如果所有普通消息都套用重型项目流程，Chat/Operation 会变慢。

Adaptive Task Protocol Runtime 为 Project 类型任务提供独立的协议账本和 Gate，同时让 Chat、Operation 继续走 Hermes 原有轻量路径。

## 预期效果

- 每个 Project 有唯一的 `task_id`、结构化 Plan、Todo 和 Acceptance Criteria；
- 实际文件修改、命令执行和测试仍由原来的 Hermes Profile 完成；
- Planner/Judge 是 Hermes 的受限子代理，不是 Runtime 自己启动的 Worker；
- Evidence 与当前 workspace revision 绑定，代码变化后旧证据会失效；
- 缺少证据、证据过期、验证失败或 Runtime 不可达时，Project 写入和关闭会 fail-closed；
- Runtime 重启后可以从 SQLite/JSONL 账本恢复任务状态；
- 只有确定性 Gate 通过后，任务才允许进入 `CLOSED`。

## 设计架构

```text
                    ┌──────────────────────────┐
User / Feishu / CLI │   当前 Hermes Profile    │
        ───────────▶│  原会话 + 原有工具执行   │
                    └────────────┬─────────────┘
                                 │ public subagent lifecycle
                    ┌────────────▼─────────────┐
                    │      Bridge Plugin       │
                    │ Chat/Operation/Project   │
                    │ pre-LLM context          │
                    │ pre-tool fail-closed     │
                    └────────────┬─────────────┘
                                 │ HTTP loopback + Bearer token
                    ┌────────────▼─────────────┐
                    │ Adaptive Runtime          │
                    │ Contract / Plan / Todo    │
                    │ Evidence / Verifier      │
                    │ Gate / Close             │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │ SQLite 状态 + JSONL 审计  │
                    │ 本地 runtime-data         │
                    └──────────────────────────┘
```

### 责任边界

Runtime 负责：

- Task Contract、Plan、Todo、Acceptance Criteria；
- 生命周期和状态转换；
- Evidence 完整性、SHA-256 和 revision 新鲜度；
- 确定性 Verifier 和 Gate；
- 任务恢复、审计和关闭授权。

Hermes Profile 负责：

- 接收用户请求；
- 读取文件、修改代码、执行命令和运行测试；
- 通过 Hermes 子代理提供 Planner/Judge 能力；
- 根据 Runtime 返回的返工项继续执行。

Runtime **不会**：

- 配置独立 LLM；
- 直接调用外部模型 API；
- 启动另一个 Hermes/Codex Worker；
- 直接修改项目文件；
- 直接执行用户项目命令；
- 修改 Hermes 上游核心代码。

## 快速开始：Agent 模式

Agent 模式适合把 Runtime 接入 Hermes，让当前 Profile 自动执行 Project 的 Plan→Do→Verify→Gate 流程。

### 1. 安装 Runtime

要求 Python 3.12 或更高版本：

```bash
python -m pip install .
python -m adaptive_runtime --help
```

开发环境也可以使用 editable 安装：

```bash
python -m pip install -e .
```

### 2. 初始化本地数据目录

数据目录只用于本机运行，不应提交到 Git：

```bash
python -m adaptive_runtime --data-dir ./runtime-data init
```

该命令会创建本地 SQLite/JSONL 目录和 Bearer token。不要把 token 写入 README、日志、Issue 或版本库。

### 3. 启动 Runtime

```bash
python -m adaptive_runtime --data-dir ./runtime-data serve
```

默认只监听 loopback：

```text
http://127.0.0.1:8790
```

### 4. 安装并启用 Bridge

先验证 Bridge manifest 和导入：

```bash
hermes plugins doctor ./bridge --ci
```

然后按照所使用 Hermes 版本的用户插件安装方式注册 `bridge/`，并只在需要的 Profile 中启用：

```yaml
plugins:
  enabled:
    - adaptive-task-protocol
  entries:
    adaptive-task-protocol:
      allow_tool_override: false
      settings:
        runtime_url: http://127.0.0.1:8790
        token_file: /path/to/runtime-data/auth-token
        request_timeout: 5.0
        subagent_wait_timeout: 600.0
```

`token_file` 必须指向本机 Runtime 初始化生成的 token 文件。路径应由安装者根据自己的环境填写；不要复制任何其他机器的绝对路径。

### 5. Agent 流程

启用 Bridge 后，典型 Project 流程是：

```text
用户提出复杂交付请求
→ Bridge 分类为 Project Candidate
→ adaptive_task_start
→ Planner 返回结构化 Plan/Todo/AC
→ 当前 Hermes Profile 执行实际工作
→ adaptive_task_event 记录阶段和 Todo
→ 提交 Evidence
→ Runtime 执行 Gate
→ PASS 后才允许 close
```

Planner/Judge 的 JSON 结果只是候选建议，不能绕过 Runtime Evidence 和 Gate。

## 快速开始：人模式

人模式适合不接 Hermes、直接通过 HTTP API 驱动 Runtime，或用于集成测试和自定义编排器。

### 1. 健康检查

```bash
curl http://127.0.0.1:8790/health
```

预期结果：

```json
{"ready": true, "protocol_version": "1.0"}
```

### 2. 创建一个 Project Task

下面是最小示例。`task_id`、workspace path 和 revision 应替换成调用者自己的值：

```bash
curl -X POST http://127.0.0.1:8790/v1/tasks \
  -H "Authorization: Bearer <local-runtime-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "protocol_version": "1.0",
    "contract_version": 1,
    "task_id": "example-task",
    "mode": "project",
    "objective": "完成一个可验证的示例任务",
    "non_goals": ["不修改任务范围之外的文件"],
    "workspace": {"path": "./example-project", "revision": "initial-revision"},
    "state": "CREATED",
    "plan_id": null,
    "todo": [{
      "id": "T1",
      "title": "实现并验证示例功能",
      "depends_on": [],
      "verification": "运行声明的测试并保存结果"
    }],
    "acceptance_criteria": [{
      "id": "AC1",
      "description": "声明的测试命令成功完成",
      "kind": "machine_verifiable",
      "verifier": {"type": "test", "commands": ["python -m pytest -q"]},
      "required_evidence": ["command", "exit_code", "test_report", "workspace_revision"],
      "required": true
    }]
  }'
```

### 3. 查询、提交和关闭

```text
GET  /v1/tasks/{task_id}
POST /v1/tasks/{task_id}/plan
POST /v1/tasks/{task_id}/events
POST /v1/tasks/{task_id}/evidence
POST /v1/tasks/{task_id}/evaluate
POST /v1/tasks/{task_id}/rework
POST /v1/tasks/{task_id}/close
POST /v1/tasks/{task_id}/subagents
```

人模式必须自己遵守状态顺序，并为每个必需 Acceptance Criterion 提供完整 Evidence。直接发送一个 `PASS` 或 `closed=true` 不会绕过 Gate。

## 状态机

```text
CREATED → PLANNING → READY → EXECUTING → SELF_CHECK → REVIEWING
                                               ├→ REWORK_REQUIRED → EXECUTING
                                               ├→ BLOCKED
                                               └→ GATE_PASSED → CLOSED
```

常见规则：

- Planner 无法生成合法 Plan 时保持 `PLANNING/BLOCKED`；
- Todo 生命周期事件会同步持久化 Todo status；
- 验证后修改 workspace 会使相关 Evidence 失效；
- 缺失或过期 Evidence 时不能关闭；
- Runtime 不可达时 Project 写入 fail-closed；
- Chat/Operation 不创建重型 Project Contract。

## 可以产生的效果

例如，一个通用的软件或内容交付任务可以形成如下可审计结果：

```text
Task: example-task
Plan: plan-example
Todo: 5 项，其中 5 项完成
AC: 4 项，其中 4 项 PASS
Evidence: 测试报告、构建结果、启动 readiness、版本指纹
Gate: PASS
Final state: CLOSED
```

这些只是格式示例，不代表任何特定本机任务、Profile、路径、模型或运行数据。

## 可能存在的问题和限制

### 复杂任务可能长时间不结束

Project 模式会反复执行：

```text
执行 → 自检 → 测试 → Evidence → Gate → 返工 → 再验证
```

因此一个较复杂的 Task 可能长时间处于 `EXECUTING`、`REWORK_REQUIRED` 或 `REVIEWING`，这是验证闭环的代价，不一定是死循环。应通过 Task snapshot、Todo、最近事件和 Evidence 判断是否正在推进。

### Planner/Judge 依赖宿主模型

Runtime 本身不拥有 LLM。Planner/Judge 的延迟、限流、连接失败和模型输出格式都会受宿主 Hermes Provider 影响。子代理失败时，Runtime 保持安全状态，不应通过静默切换 Provider 来掩盖问题。

### Evidence 会因代码变化失效

提交 Evidence 后再次修改 workspace，旧 revision 可能不再有效。需要重新运行受影响的测试并提交新 Evidence。

### 进程重启边界

Runtime 能恢复持久化任务账本，但正在执行中的 Hermes 子代理句柄不保证跨进程恢复。进程重启后应重新查询 Task；未确认的子代理结果必须视为 `UNKNOWN`，不能伪造成功。

### 本地服务边界

默认服务只监听本机 loopback，适合单机 Hermes 集成。它不是远程多租户任务服务器，也没有为公网暴露设计。

### Chat/Operation 与 Project 的边界需要清晰

单次查询、分析、读取或轻量操作不应被强制升级为 Project。只有明确的多阶段交付、持续迭代、构建/测试/部署或验收范围才适合 Project。

## 安全与隐私

以下内容只应存在于本机运行环境，不应提交：

```text
runtime-data/
SQLite 数据库
JSONL 事件和子代理记录
运行日志
Bearer token
.env 文件
本机 Profile 配置
Windows Startup 脚本
备份和测试运行产物
```

Runtime 的 `/health` 不返回任务数据；`/v1` 接口需要 Bearer token；Bridge 默认禁止工具覆盖。请在部署前检查 Git tree 和历史，确认没有本机路径、token、日志或运行数据进入发布内容。

## 代码布局

```text
src/adaptive_runtime/       # Runtime 协议、账本、Evidence、Verifier、Gate、HTTP 服务
bridge/                     # Hermes Bridge、分类、Hook、Runtime client、Planner/Judge adapter
schemas/                    # 对外协议 Schema
pyproject.toml              # Python 包和 CLI 元数据
README.md                   # 发布说明
```

本仓库不依赖独立模型服务，也不要求修改 Hermes 上游源码。

## License

MIT
