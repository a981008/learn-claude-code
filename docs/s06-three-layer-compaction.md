# s06 三层压缩详解

## 架构概览

```
Tool call result
       │
       ▼
┌─────────────────────────────┐
│ Layer 1: micro_compact      │  ← 每轮静默执行
│ 保留最近 3 条工具结果，      │
│ 其余替换为 "[Previous: used {tool}]" │
└─────────────────────────────┘
       │
       ▼
   token > 50000 ?
    │         │
   否         是
    │         ▼
  继续    ┌─────────────────────────────┐
         │ Layer 2: auto_compact       │  ← 自动触发
         │ 完整 transcript 写入磁盘，   │
         │ LLM 总结对话，messages 替换  │
         │ 为 [summary]                 │
         └─────────────────────────────┘
                    │
                    ▼
         ┌─────────────────────────────┐
         │ Layer 3: compact tool        │  ← 手动触发
         │ agent 主动调用 compact 工具， │
         │ 立即触发总结（同 Layer 2）   │
         └─────────────────────────────┘
```

## Layer 1: `micro_compact` — 轻量级原位替换

每轮 agent_loop 开始时调用，直接修改原 messages 列表，不申请新 token。

### 压缩逻辑

```
messages: [user, assistant, user(tool_result), assistant, user(tool_result), ...]
                                         ↑______过时结果______↑
                                           保留最近 3 条
```

1. **收集**：遍历 messages，定位所有 `tool_result` 项，记录其 `(msg_idx, part_idx, result_dict)` 位置
2. **映射**：遍历 assistant 消息，建立 `tool_use_id → tool_name` 映射（用于生成占位符文本）
3. **替换**：倒数第 4 条及更早的 tool_result，其内容替换为 `[Previous: used {tool_name}]`
4. **跳过**：`read_file` 结果永远保留（它是参考材料，压缩它会迫使 agent 重新读文件）

### 关键代码

```python
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {"read_file"}

def micro_compact(messages: list) -> list:
    # 收集所有 tool_result
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))

    if len(tool_results) <= KEEP_RECENT:
        return messages  # 不需要压缩

    # 建立 tool_use_id → tool_name 映射
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name

    # 替换旧结果（保留最近 KEEP_RECENT 条）
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        tool_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue  # read_file 不压缩
        result["content"] = f"[Previous: used {tool_name}]"

    return messages
```

### 特点

- **无 token 开销**：直接修改原消息，不调用 LLM
- **有选择保留**：`read_file` 结果不被压缩（参考材料价值高）
- **渐进式**：每轮只压缩掉 1 条旧结果，不会突然丢失大量上下文

---

## Layer 2: `auto_compact` — 溢出触发的有损压缩

当 `estimate_tokens(messages) > 50000` 时触发，将完整历史存档为文件，对话窗口压缩为 1 条摘要。

### 压缩逻辑

1. **存档**：完整 messages 以 JSONL 格式写入 `.transcripts/transcript_{timestamp}.jsonl`
2. **总结**：截取最后 80000 字符，调用 LLM 生成三段式摘要
3. **替换**：messages 被替换为单条摘要消息（完整历史丢失，仅磁盘保留）

### 关键代码

```python
THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

def auto_compact(messages: list) -> list:
    # Step 1: 完整存档
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")

    # Step 2: LLM 总结（截取最后 80000 字符防止爆 token）
    conversation_text = json.dumps(messages, default=str)[-80000:]
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = next((block.text for block in response.content
                    if hasattr(block, "text")), "")

    # Step 3: 替换为单条摘要消息
    return [
        {"role": "user", "content":
            f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
    ]
```

### 调用条件

```python
def agent_loop(messages: list):
    while True:
        micro_compact(messages)  # Layer 1

        # Layer 2: 溢出触发
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)

        response = client.messages.create(...)
        # ...
```

### 特点

- **有损压缩**：N 条消息变成 1 条，对话历史窗口内的细节永久丢失
- **完整存档**：磁盘保留原始完整记录，可供事后查阅
- **token 触发**：防止 context 窗口溢出（50k token 阈值）

---

## Layer 3: `compact` tool — agent 主动触发的即时压缩

agent 通过调用 `compact` 工具主动触发总结，逻辑同 Layer 2，但触发权在 agent 自身。

### 压缩逻辑

与 Layer 2 完全相同，区别仅在于**触发时机由 agent 决定**。

### 关键代码

```python
def agent_loop(messages: list):
    while True:
        # ...
        response = client.messages.create(...)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True  # 标记
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(output)})

        messages.append({"role": "user", "content": results})

        # Layer 3: 手动触发
        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)
            return  # 本轮直接结束，不返回结果给用户
```

### 工具定义

```python
TOOLS = [
    # ... bash, read_file, write_file, edit_file ...
    {"name": "compact",
     "description": "Trigger manual conversation compression.",
     "input_schema": {"type": "object",
                      "properties": {
                          "focus": {"type": "string",
                                    "description": "What to preserve in the summary"}
                      }}},
]
```

### 特点

- **主动遗忘**：agent 可根据当前对话状态判断"该整理记忆了"，而非被动等待溢出
- **同一逻辑**：底层调用 `auto_compact()`，存档 + 摘要机制完全一致
- **提前返回**：压缩后本轮立即结束，不向用户返回结果（agent 选择在适当时机整理）

---

## 三层协作流程

```
每轮 agent_loop():
│
├─ micro_compact(messages)         # Layer 1: 每轮执行，修剪旧 tool_result
│
├─ estimate_tokens > 50000 ?
│   └─ yes → auto_compact()       # Layer 2: 溢出触发，总结替换
│
├─ LLM generate → tool calls
│   └─ 调用 compact tool →         # Layer 3: 主动触发
│         auto_compact()           #      同 Layer 2 逻辑
```

## 对比总结

| | Layer 1: micro_compact | Layer 2: auto_compact | Layer 3: compact tool |
|---|---|---|---|
| **触发时机** | 每轮 | token > 50000 | agent 手动调用 |
| **压缩粒度** | tool_result 内容 | 全部 messages | 全部 messages |
| **是否调用 LLM** | 否 | 是（做摘要） | 是（做摘要） |
| **历史存档** | 否 | `.transcripts/*.jsonl` | `.transcripts/*.jsonl` |
| **结果** | 修改原消息 | 替换为 1 条摘要 | 替换为 1 条摘要 |
| **信息保留** | `read_file` 永远保留 | 只保留摘要 | 只保留摘要 |

## 设计洞察

> "The agent can forget strategically and keep working forever."

- **micro_compact** 是"静默渐进式清理"，每轮清理一条旧结果，保持 context 轻量但不丢失阅读材料
- **auto_compact** 是"有损备份"，完整记录落盘但对话窗口只保留摘要，防止 context 溢出
- **compact tool** 是"主动遗忘"，让 agent 自身决定何时该整理记忆——这是一种元认知能力
