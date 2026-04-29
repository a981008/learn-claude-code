#!/usr/bin/env python3
"""
s02_tool_use_debug.py - Tool Agent with full trace visibility

增强点：
- 👀 可视化 LLM thinking / tool call / tool result
- 🧾 清晰展示 messages 流转
- 🔍 调试 agent 执行全过程
"""

import os
import subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# =========================
# 👀 DEBUG 工具
# =========================
def debug(title: str, content):
    print("\n" + "=" * 20 + f" {title} " + "=" * 20)
    print(content)


# =========================
# 🔐 安全路径
# =========================
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# =========================
# 🧠 Tool implementations
# =========================
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_ls(path: str) -> str:
    try:
        entries = safe_path(path).iterdir()
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)
    except Exception as e:
        return f"Error: {e}"

def run_read(path: str, limit: int = 50000) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()

        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]

        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()

        if old_text not in content:
            return f"Error: Text not found in {path}"

        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# =========================
# 🧭 Tool Router
# =========================
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit", 50000)),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "ls": lambda **kw: run_ls(kw["path"]),
}


# =========================
# 🧰 Tool schema
# =========================
from anthropic.types import ToolParam 
TOOLS:list[ToolParam] = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"}
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "ls",
        "description": "List directory contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": ["path"]
        }
    }
]


# =========================
# 🤖 Agent Loop（可观测版）
# =========================
def agent_loop(messages: list):
    while True:

        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        debug("LLM OUTPUT", response.content)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        tool_results = []

        for block in response.content:

            # 🧠 thinking
            if block.type == "thinking":
                debug("THINKING", block.thinking)

            # 🔧 tool call
            if block.type == "tool_use":

                debug(
                    "TOOL CALL",
                    f"{block.name}({block.input})"
                )

                handler = TOOL_HANDLERS.get(block.name)

                if handler:
                    output = handler(**block.input)
                else:
                    output = f"Unknown tool: {block.name}"

                debug("TOOL RESULT", output)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })

        messages.append({"role": "user", "content": tool_results})


# =========================
# 🧑 CLI 入口
# =========================
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36muser >> \033[0m")
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
        print("\n")