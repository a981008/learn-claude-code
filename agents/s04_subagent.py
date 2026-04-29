#!/usr/bin/env python3
# Harness: context isolation -- protecting the model's clarity of thought.
# 👀 可观测性增强: 添加 debug 追踪、thinking 显示、工具调用可视化
"""
s04_subagent.py - Subagents with full trace visibility

继承自 s03 的可观测性增强：
- 👀 可视化 LLM thinking / tool call / tool result
- 🧾 清晰展示 messages 流转
- 🔍 调试 agent 执行全过程
- 📊 子代理调用也可见

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

Key insight: "Process isolation gives context isolation for free."
"""

# =========================
# 1. 导入和初始化
# =========================
import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 如果使用了自定义 base_url，则移除 auth token（base_url 自行处理认证）
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# SYSTEM 消息：父 agent 知道使用 task 工具来派发子任务
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
# SUBAGENT_SYSTEM 消息：子 agent 专注于完成任务并返回摘要
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


# =========================
# 👀 DEBUG 工具
# =========================
def debug(title: str, content):
    """统一调试输出格式，便于追踪 agent 执行全过程"""
    print("\n" + "=" * 20 + f" {title} " + "=" * 20)
    print(content)


# =========================
# 2. 工具实现（父和子 agent 共享）
# =========================
def safe_path(p: str) -> Path:
    """将相对路径解析为绝对路径，并检查是否在 WORKDIR 内（防止路径穿越）"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 shell 命令，带安全检查和超时限制"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

from typing import Optional
def run_read(path: str, limit: Optional[int] = None) -> str:
    """读取文件内容，可选限制行数（避免一次性加载大文件）"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """写入文件内容，自动创建父目录"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确替换文件中的一段文本（只替换第一次出现）"""
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 工具名称到处理函数的映射，供 run_subagent 和 agent_loop 共用
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

from anthropic.types import ToolParam
# =========================
# 3. 工具定义
# =========================
# 子 agent 的工具列表：拥有基础工具，但不能派发子任务（避免递归）
CHILD_TOOLS:list[ToolParam] = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

from anthropic.types import MessageParam

# =========================
# 4. 子 agent 执行函数
# =========================
def run_subagent(prompt: str) -> str:
    """
    运行子 agent：独立上下文，独立工具集，执行后只返回文本摘要。

    设计原则：
    - 独立上下文：sub_messages 从零开始，不继承父 agent 的 history
    - 独立工具集：只能使用 CHILD_TOOLS（无 task 工具）
    - 有限轮次：最多 30 轮，防止无限循环
    - 摘要返回：只返回最终文本，不返回中间过程
    """
    debug("SUBAGENT START", f"Dispatching subagent with prompt:\n{prompt[:200]}...")

    # 独立的消息列表，从用户提示开始
    sub_messages:list[MessageParam] = [{"role": "user", "content": prompt}]

    for round_num in range(30):  # 安全限制，防止无限循环
        debug(f"SUBAGENT ROUND {round_num + 1}", f"Messages count: {len(sub_messages)}")

        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )

        debug(f"SUBAGENT LLM OUTPUT [Round {round_num + 1}]", response.content)
        sub_messages.append({"role": "assistant", "content": response.content})

        # 非 tool_use 停止时，说明子 agent 已完成任务
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:

            # 🧠 thinking 块：LLM 的思考过程（如果启用）
            if block.type == "thinking":
                debug(f"SUBAGENT THINKING [Round {round_num + 1}]", block.thinking)

            # 🔧 tool_use 块：调用工具
            if block.type == "tool_use":
                debug(f"SUBAGENT TOOL CALL [Round {round_num + 1}]", f"{block.name}({block.input})")

                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"

                debug(f"SUBAGENT TOOL RESULT [Round {round_num + 1}]", output)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})

        sub_messages.append({"role": "user", "content": results})

    # 只将最终文本返回给父 agent，子 agent 的全部上下文被丢弃
    final_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text") or "(no summary)"
    debug("SUBAGENT END", f"Returning summary:\n{final_text[:500]}")
    return final_text


# =========================
# 5. 父 agent 工具和主循环
# =========================
# 父 agent 的工具列表：在子工具基础上增加 task 工具用于派发子任务
PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short description of the task"}}, "required": ["prompt"]}},
]


def agent_loop(messages: list):
    """
    父 agent 主循环：
    - 接收 messages（包含历史对话）
    - 调用 LLM，根据 stop_reason 决定是否处理工具调用
    - 遇到 task 工具调用时，派发子 agent
    - 工具结果追加到 messages，继续下一轮
    """
    round_num = 0
    while True:
        round_num += 1
        debug(f"AGENT MESSAGES [Round {round_num}]", messages)

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )

        debug(f"LLM OUTPUT [Round {round_num}]", response)
        messages.append({"role": "assistant", "content": response.content})

        # 非 tool_use 停止时，说明本轮对话结束
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:

            # 🧠 thinking 块
            if block.type == "thinking":
                debug(f"THINKING [Round {round_num}]", block.thinking)

            # 🔧 tool_use 块
            if block.type == "tool_use":
                debug(f"TOOL CALL [Round {round_num}]", f"{block.name}({block.input})")

                if block.name == "task":
                    # task 工具：派发子 agent，子 agent 完成后返回摘要
                    desc = block.input.get("description", "subtask")
                    prompt = block.input.get("prompt", "")
                    debug(f"TASK DISPATCH [Round {round_num}]", f"description: {desc}")
                    output = run_subagent(prompt)
                else:
                    # 其他工具：直接在父进程执行
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"

                debug(f"TOOL RESULT [Round {round_num}]", output)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

        messages.append({"role": "user", "content": results})


# =========================
# 6. 入口：交互式 REPL
# =========================
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        debug("USER INPUT", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        debug("FINAL ASSISTANT", response_content)
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(f"\033[32massistant >> {block.text}\033[0m")
        print()
