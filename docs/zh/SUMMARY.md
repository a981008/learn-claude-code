# docs/zh 关键技巧总结

## s01 - Agent 循环
- **while True + stop_reason != "tool_use"** 控制整个流程，30行代码实现完整 Agent
- 工具调用结果作为 user 消息追加，实现多轮对话
- 模型停止调用工具时循环结束

## s02 - 工具使用
- **dispatch map 模式**: `{tool_name: handler}` 字典替代 if/elif 链，加工具只需加 handler
- `safe_path()` 路径沙箱，防止操作逃逸工作区

## s03 - TodoWrite
- **强制单一 in_progress**：同时只能有一个任务进行中，确保顺序聚焦
- **Nag reminder**：连续3轮不更新 todo 则自动注入提醒
- 先计划后执行，完成率翻倍

## s04 - Subagent
- Subagent 以空消息列表启动，**不污染父 Agent 上下文**
- 30+次工具调用后父 Agent 只收到一段**摘要文本**
- Subagent 工具集**禁止递归**调用 `task` 工具

## s05 - Skill 加载
- **两层架构**：Skill 名称放系统提示（便宜），tool_result 按需放完整内容（贵）
- SKILL.md + YAML frontmatter 定义元数据

## s06 - 上下文压缩
- **三层压缩**：micro_compact（每次调用前）→ auto_compact（token 超阈值）→ manual compact（显式调用）
- 完整历史通过 .transcripts/ 保存磁盘，可恢复

## s07 - 任务系统
- **DAG 任务图**：JSON 文件含 `blockedBy` 依赖关系
- 任务状态存储磁盘，压缩和重启后仍存活

## s08 - 后台任务
- 慢命令（npm install、pytest）丢后台，Agent 继续处理其他工作
- 每次 LLM 调用前排空通知队列，线程安全（threading.Lock）

## s09 - Agent 团队
- **JSONL 邮箱**：append-only 收件箱实现持久化通信
- config.json 名册维护团队成员状态
- 每 spawn 一个队友启动一个独立线程

## s10 - 团队协议
- **request_id 关联**：请求和响应通过唯一 ID 配对
- **FSM 模式**：`pending -> approved | rejected`
- 握手安全关机：领导请求 + 队友批准，避免脏状态

## s11 - 自主 Agent
- 队友**自组织认领**：扫描看板，认领 pending、无 owner、未被阻塞的任务
- **WORK/IDLE 双阶段**：停止调用工具时进入 IDLE，轮询收件箱
- 上下文过短时**重注入 identity block**，防止忘记自己是谁
- **60秒空闲超时**自动关机

## s12 - Worktree 任务隔离
- **git worktree 并行**：每个任务独立分支 `wt/xxx`，彻底避免文件冲突
- 任务 ID 同时绑定任务状态和 worktree 目录
- 事件流日志 `.worktrees/events.jsonl`，崩溃后可重建
