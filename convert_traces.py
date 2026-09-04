#!/usr/bin/env python3
# convert_traces.py
#
# Convert TeichAI Ox-Alpha Pi agent traces so that every file-edit /
# file-create tool call (write / edit) becomes an MCP tool call through
# the prime-agent "tools" server (which routes to Emacs via python-bridge).
#
# Trace format (pi/teich), one JSON object per line:
#   {"type": "session" | "model_change" | "thinking_level_change" | "custom", ...}
#   {"type": "message", "message": {...}}   # roles: user / assistant / toolResult
#
# Assistant messages contain content blocks of type thinking | text | toolCall.
# toolResult messages carry toolCallId / toolName / content[] / isError.
#
# WHAT THIS CONVERTER DOES:
#   - keeps session metadata lines untouched;
#   - maps `write(path, content)` -> `ox-bridge-write` MCP tool;
#   - maps `edit(path, [{oldText, newText}, ...])` -> `ox-bridge-edit` MCP tool;
#   - maps `read(path)` -> `ox-bridge-read` MCP tool;
#   - leaves `bash` calls untouched (or converts to await bash());
#   - for ipython mode: all bridge calls become `await mcp.call_tool("tools", ...)`;
#   - prepends `mcp.list_tools("tools")` discovery before the first bridge call of each trace;
#   - inserts a `bridgePlan` audit event before each rewritten assistant message;
#   - rewrites matching toolResult messages to the bridge reply shape;
#   - optionally prepends a system prompt.
#
# Usage:
#     python convert_traces.py SRC_DIR DST_DIR [--system-prompt PATH] [--limit N]
#
# Generated code templates (REPR for Python literals, not call sites):
#   - write: result = await mcp.call_tool("tools", "ox-bridge-write", {...}); print(result)
#   - edit:  result = await mcp.call_tool("tools", "ox-bridge-edit", {...}); print(result)
#   - read:  result = await mcp.call_tool("tools", "ox-bridge-read", {...}); print(result)
#
# Tool schemas (exported for dataset's tool_schema field):
#   - ox-bridge-write: {path: string, content: string}
#   - ox-bridge-edit:  {path: string, edits: [{old: string, new: string}]}
#   - ox-bridge-read:  {path: string, start?: int, end?: int}

from __future__ import annotations

import argparse
import importlib.resources
import json
import pathlib
import textwrap
from typing import Any

import dataclasses


# =============================================================================
# TOOL SCHEMA (for the dataset's tool_schema field)
# =============================================================================

TOOL_SCHEMA = {
    "tools": [
        {
            "name": "ox-bridge-write",
            "description": (
                "Create or overwrite a file at the given path, inside Emacs. "
                "Returns {status: 'ok', bytes: N, path: PATH} on success."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "content": {"type": "string", "description": "File content to write"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "ox-bridge-edit",
            "description": (
                "Apply a list of edits to a file inside Emacs. "
                "Each edit has 'old' (text to find) and 'new' (replacement). "
                "Returns {status: 'ok', path: PATH, hunks: N} on success."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                            "required": ["old", "new"],
                        },
                        "description": "List of {old, new} edit hunks",
                    },
                },
                "required": ["path", "edits"],
            },
        },
        {
            "name": "ox-bridge-read",
            "description": (
                "Read a region of a file from Emacs. Indexes are character-based "
                "(inclusive start, exclusive end). Returns the file contents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "start": {"type": "integer", "description": "Start index (inclusive)"},
                    "end": {"type": "integer", "description": "End index (exclusive)"},
                },
                "required": ["path"],
            },
        },
    ]
}

# =============================================================================
# GENERATED CODE HELPERS (MCP-style calls, NO import lines)
# =============================================================================

# Discovery code (insert once per trace, before first bridge call)
DISCOVERY_CODE = textwrap.dedent("""
    tools_list = await mcp.list_tools("tools")
    for t in tools_list:
        print(t["name"], t["inputSchema"])
""").strip()


def code_write(path: str, content: str) -> str:
    """Generate ipython cell code for ox-bridge-write using mcp.call_tool."""
    args_js = json.dumps({"path": path, "content": content}, ensure_ascii=False)
    return (
        f'result = await mcp.call_tool("tools", "ox-bridge-write", {args_js})\n'
        f'print(result)\n'
    )


def code_edit(path: str, edits: list[dict]) -> str:
    """Generate ipython cell code for ox-bridge-edit using mcp.call_tool."""
    normalized = [{"old": e.get("old", e.get("oldText", "")), "new": e.get("new", e.get("newText", ""))} for e in edits]
    args_js = json.dumps({"path": path, "edits": normalized}, ensure_ascii=False)
    return (
        f'result = await mcp.call_tool("tools", "ox-bridge-edit", {args_js})\n'
        f'print(result)\n'
    )


def code_read(path: str, start: int | None = None, end: int | None = None) -> str:
    """Generate ipython cell code for ox-bridge-read using mcp.call_tool."""
    args = {"path": path}
    if start is not None:
        args["start"] = start
    if end is not None:
        args["end"] = end
    args_js = json.dumps(args, ensure_ascii=False)
    return (
        f'result = await mcp.call_tool("tools", "ox-bridge-read", {args_js})\n'
        f'print(result)\n'
    )


def code_bash(cmd: str) -> str:
    """Generate ipython cell code for bash call."""
    return f"await bash({cmd!r})\n"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclasses.dataclass
class ConvertStats:
    wrote: int = 0
    edited: int = 0
    readed: int = 0
    bash_passthrough: int = 0
    results_rewritten: int = 0


# =============================================================================
# TOOL CALL CONVERSION
# =============================================================================

def convert_tool_call(tc: dict, stats: ConvertStats, mode: str = "ipython") -> dict | None:
    """Convert a single toolCall to bridge format. Returns None for passthrough."""
    name = tc.get("name")
    args = tc.get("arguments") or {}
    tid = tc.get("id")

    if name == "write":
        path = args.get("path")
        content = args.get("content")
        if not path or not isinstance(content, str):
            return None  # malformed: keep original
        stats.wrote += 1
        code = code_write(path, content)
        if mode == "tools":
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ox-bridge-write",
                "arguments": {
                    "path": path,
                    "content": content,
                    "bridge_source": code,
                },
            }
        else:  # ipython
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ipython",
                "arguments": {"code": code},
            }

    if name == "edit":
        edits = args.get("edits")
        path = args.get("path")
        # Some traces embed edits as a JSON string; normalize to list of dicts.
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except json.JSONDecodeError:
                edits = None
        ok = (
            path
            and isinstance(edits, list)
            and edits
            and all(isinstance(h, dict) and ("oldText" in h or "old" in h) for h in edits)
        )
        if not ok:
            return None  # malformed: keep original
        stats.edited += 1
        code = code_edit(path, edits)
        if mode == "tools":
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ox-bridge-edit",
                "arguments": {
                    "path": path,
                    "edits": args["edits"],
                    "bridge_source": code,
                },
            }
        else:
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ipython",
                "arguments": {"code": code},
            }

    if name == "read":
        path = args.get("path")
        if not path:
            return None
        stats.readed += 1
        code = code_read(path, args.get("start"), args.get("end"))
        if mode == "tools":
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ox-bridge-read",
                "arguments": {
                    "path": path,
                    "start": args.get("start"),
                    "end": args.get("end"),
                    "bridge_source": code,
                },
            }
        else:
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ipython",
                "arguments": {"code": code},
            }

    if name == "bash":
        cmd = args.get("command", "") if args else ""
        if mode == "ipython":
            stats.bash_passthrough += 1
            return {
                "type": "toolCall",
                "id": tid,
                "name": "ipython",
                "arguments": {"code": code_bash(cmd)},
            }
        return None  # bash passthrough untouched in tools mode

    # Unknown tool type: leave as-is
    return None


# =============================================================================
# BRIDGE PLAN GENERATION
# =============================================================================

def create_bridge_plan(tool_name: str, path: str) -> dict:
    """Create a bridgePlan audit event for a tool call."""
    return {
        "type": "bridgePlan",
        "id": f"plan-{tool_name}",
        "parentId": None,  # filled by caller
        "timestamp": None,  # filled by caller
        "tool": tool_name,
        "python_mcp_call": f'mcp.call_tool("tools", "{tool_name}", ...)',
        "summary": f"{tool_name} -> {path}",
    }


# =============================================================================
# TRACE CONVERSION
# =============================================================================

def convert_trace(
    src_p: pathlib.Path,
    dst_p: pathlib.Path,
    system_prompt: str | None = None,
    mode: str = "ipython",
) -> ConvertStats:
    stats = ConvertStats()
    bridged_ids: dict[str, str] = {}  # toolCallId -> bridge tool name
    out: list[str] = []
    emitted_system = False
    needs_discovery = True  # emit discovery before first bridge call

    for raw in src_p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)

        # Passthrough non-message lines
        if obj.get("type") != "message":
            out.append(raw)
            continue

        msg = obj["message"]
        role = msg.get("role")

        # Inject system prompt as first message
        if system_prompt and not emitted_system and role in ("user", "assistant"):
            out.append(json.dumps({
                "type": "message",
                "id": "system-prompt",
                "parentId": obj.get("id"),
                "timestamp": 0,
                "message": {
                    "role": "system",
                    "content": [{"type": "text", "text": system_prompt}],
                    "timestamp": 0,
                },
            }, ensure_ascii=False))
            emitted_system = True

        if role != "assistant":
            out.append(json.dumps(obj, ensure_ascii=False))
            continue

        # Process assistant message
        new_content: list[dict] = []
        has_bridge_call = False

        for block in msg.get("content", []):
            if block.get("type") != "toolCall":
                new_content.append(block)
                continue

            nb = convert_tool_call(block, stats, mode)
            if nb is None:
                new_content.append(block)
                continue

            has_bridge_call = True
            tool_display = nb["name"] if mode == "tools" else "ipython"
            path_for_plan = nb["arguments"].get("path", "N/A")
            bridged_ids[block["id"]] = tool_display

            # Insert discovery code before first bridge call (ipython mode)
            if needs_discovery and mode == "ipython":
                out.append(json.dumps({
                    "type": "toolCall",
                    "id": block["id"] + "-discovery",
                    "parentId": obj["id"],
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": DISCOVERY_CODE}],
                    },
                }, ensure_ascii=False))
                needs_discovery = False

            # Insert bridgePlan audit event
            out.append(json.dumps({
                "type": "bridgePlan",
                "id": "plan-" + block["id"],
                "parentId": obj["id"],
                "timestamp": obj.get("timestamp"),
                "tool": nb["name"],
                "summary": f"{nb['name']} -> {path_for_plan}",
            }, ensure_ascii=False))

            new_content.append(nb)

        # Rewrite the assistant message
        new_msg = dict(msg, content=new_content)
        out.append(json.dumps({"type": "message", "id": obj["id"], "message": new_msg}, ensure_ascii=False))

    # Rewrite toolResult messages
    # (second pass: re-read and fix toolResults)
    final_out: list[str] = []
    for line in out:
        obj = json.loads(line)
        if obj.get("type") == "message" and obj["message"].get("role") == "toolResult":
            tid = obj["message"].get("toolCallId")
            if tid in bridged_ids:
                stats.results_rewritten += 1
                tool_name = bridged_ids[tid]
                orig_content = ""
                for c in obj["message"].get("content", []):
                    if c.get("type") == "text":
                        orig_content = c.get("text", "")
                        break
                obj["message"] = dict(
                    obj["message"],
                    toolName=tool_name,
                    content=[{"type": "text", "text": ";; bridge result\nstatus: ok\n" + orig_content}],
                    bridgeRewritten=True,
                )
        final_out.append(json.dumps(obj, ensure_ascii=False))

    dst_p.parent.mkdir(parents=True, exist_ok=True)
    dst_p.write_text("\n".join(final_out) + "\n", encoding="utf-8")
    return stats


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Convert Ox-Alpha pi traces to prime-agent MCP tool calls via Emacs bridge"
    )
    p.add_argument("src", type=pathlib.Path, help="Source directory (traces/*.jsonl)")
    p.add_argument("dst", type=pathlib.Path, help="Destination directory")
    p.add_argument("--system-prompt", type=pathlib.Path, help="Path to system prompt file")
    p.add_argument("--limit", type=int, default=0, help="Process only first N files")
    p.add_argument("--mode", choices=("ipython", "tools"), default="ipython",
                   help="Output mode: ipython (REPL cells) or tools (named tool calls)")
    ns = p.parse_args(argv)

    system_prompt = ns.system_prompt.read_text(encoding="utf-8") if ns.system_prompt else None
    ns.dst.mkdir(parents=True, exist_ok=True)

    files = sorted(ns.src.glob("*.jsonl"))
    if ns.limit:
        files = files[:ns.limit]

    totals = ConvertStats()
    for i, f in enumerate(files, 1):
        stats = convert_trace(f, ns.dst / f.name, system_prompt, ns.mode)
        totals.wrote += stats.wrote
        totals.edited += stats.edited
        totals.readed += stats.readed
        totals.bash_passthrough += stats.bash_passthrough
        totals.results_rewritten += stats.results_rewritten
        if i % 250 == 0 or i == len(files):
            print(f"[{i}/{len(files)}] write={totals.wrote} edit={totals.edited} read={totals.readed} "
                  f"bash={totals.bash_passthrough} results={totals.results_rewritten}")

    # Write tool_schema to destination
    schema_path = ns.dst.parent / "tool_schema.json"
    schema_path.write_text(json.dumps(TOOL_SCHEMA, indent=2, ensure_ascii=False) + "\n")

    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
