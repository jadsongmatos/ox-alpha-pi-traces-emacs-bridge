#!/usr/bin/env python3
# convert_traces.py
#
# Convert TeichAI Ox-Alpha Pi agent traces so that every file-edit /
# file-create tool call (write / edit) becomes an Emacs-via-python-bridge
# operation, executed through get_emacs_func_result (synchronous EPC call).
#
# Trace format (pi/teich), one JSON object per line:
#   {"type": "session" | "model_change" | "thinking_level_change" | "custom", ...}
#   {"type": "message", "message": {...}}   # roles: user / assistant / toolResult
#
# Assistant messages contain content blocks of type thinking | text | toolCall.
# toolResult messages carry toolCallId / toolName / content[] / isError.
#
# What this converter does:
#   * keeps session metadata lines untouched;
#   * converts each `write` toolCall into one `ox_bridge_write` bridging call
#     whose arguments are {path, content};
#   * converts each `edit` toolCall into one `ox_bridge_edit` bridging call
#     whose arguments are {path, edits};
#   * leaves `bash` calls untouched;
#   * inserts a `bridgePlan` audit event before each rewritten assistant message;
#   * rewrites matching toolResult messages to the bridge reply shape;
#   * optionally prepends a system prompt.
#
# The bridge helper (ox_trace_bridge.el) MUST be loaded in Emacs before running
# the generated `bridge_source` code:
#
#     (load-file "/path/to/ox_trace_bridge.el")
#
# Usage:
#     python convert_traces.py SRC_DIR DST_DIR [--system-prompt PATH] [--limit N]

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
from typing import Any

NEW_WRITE_TOOL = "ox_bridge_write"
NEW_EDIT_TOOL = "ox_bridge_edit"

BRIDGE_ELISP = {
    NEW_WRITE_TOOL: '''\
(defun ox-bridge-write (path content)
  "Create or overwrite PATH with CONTENT."
  (let ((buf (generate-new-buffer " *ox-write*")))
    (unwind-protect
        (with-current-buffer buf
          (erase-buffer)
          (insert content)
          (write-file path))
      (kill-buffer buf)))
  (list :status :ok :bytes (length content)))''',

    NEW_EDIT_TOOL: '''\
(defun ox-bridge-edit (path edits-json)
  "Apply each {old,new} hunk from EDITS-JSON to PATH."
  (let ((hunks (json-parse-string edits-json
                                  :object-type (quote alist)
                                  :array-type (quote list)))
        (buf (find-file-noselect path)))
    (with-current-buffer buf
      (dolist (h hunks)
        (let ((old (alist-get (quote old) h))
              (new (alist-get (quote new) h)))
          (goto-char (point-min))
          (unless (search-forward old nil t)
            (error "hunk not found: %s" old))
          (replace-match new t t)))
      (save-buffer))
    (list :status :ok :path path :hunks (length hunks))))''',
}


def bridge_source_write(path: str, content: str) -> str:
    return (
        "from python_bridge import get_emacs_func_result\n"
        "import json\n"
        "result = get_emacs_func_result(\n"
        '    "ox-bridge-write",\n'
        f"    {path!r},\n"
        f"    {content!r},\n"
        ")\n"
        "print(json.dumps(result, ensure_ascii=False))\n"
    )


def normalize_edits(edits: list) -> list:
    """Rename pi-style hunk keys (oldText/newText) to the bridge contract (old/new)."""
    out = []
    for e in edits:
        out.append({
            "old": e.get("oldText", e.get("old", "")),
            "new": e.get("newText", e.get("new", "")),
        })
    return out


def bridge_source_edit(path: str, edits: list) -> str:
    edits_json = json.dumps(normalize_edits(edits), ensure_ascii=False)
    return (
        "from python_bridge import get_emacs_func_result\n"
        "import json\n"
        "result = get_emacs_func_result(\n"
        '    "ox-bridge-edit",\n'
        f"    {path!r},\n"
        f"    {edits_json!r},\n"
        ")\n"
        "print(json.dumps(result, ensure_ascii=False))\n"
    )


@dataclasses.dataclass
class ConvertStats:
    wrote: int = 0
    edited: int = 0
    bash_passthrough: int = 0
    results_rewritten: int = 0


def bash_to_ipython_cell(cmd: str) -> str:
    return "await bash(" + repr(cmd) + ")\n"

def convert_tool_call(tc: dict, stats: ConvertStats, mode: str = "ipython"):
    name = tc.get("name")
    args = tc.get("arguments") or {}
    if name == "write":
        if "path" not in args or "content" not in args:
            return None  # malformed write: keep original
        stats.wrote += 1
        return {
            "type": "toolCall",
            "id": tc["id"],
            "name": NEW_WRITE_TOOL,
            "arguments": {
                "path": args["path"],
                "content": args["content"],
                "bridge_source": bridge_source_write(args["path"], args["content"]),
            },
        } if mode == "tools" else {
            "type": "toolCall",
            "id": tc["id"],
            "name": "ipython",
            "arguments": {"code": bridge_source_write(args["path"], args["content"])},
        }
    if name == "edit":
        edits = args.get("edits")
        ok = (
            "path" in args
            and isinstance(edits, list)
            and all(isinstance(h, dict) and ("oldText" in h or "old" in h) for h in edits)
        )
        if not ok:
            return None  # malformed edit: keep original
        stats.edited += 1
        return {
            "type": "toolCall",
            "id": tc["id"],
            "name": NEW_EDIT_TOOL,
            "arguments": {
                "path": args["path"],
                "edits": args["edits"],
                "bridge_source": bridge_source_edit(args["path"], args["edits"]),
            },
        } if mode == "tools" else {
            "type": "toolCall",
            "id": tc["id"],
            "name": "ipython",
            "arguments": {"code": bridge_source_edit(args["path"], args["edits"])},
        }
    if name == "bash":
        stats.bash_passthrough += 1
        if mode == "ipython":
            cmd = (tc.get("arguments") or {}).get("command", "")
            return {
                "type": "toolCall",
                "id": tc["id"],
                "name": "ipython",
                "arguments": {"code": bash_to_ipython_cell(cmd)},
            }
    return None


def convert_trace(src_p: pathlib.Path, dst_p: pathlib.Path, system_prompt=None, mode: str = "ipython") -> ConvertStats:
    stats = ConvertStats()
    bridged: dict[str, str] = {}
    out: list[str] = []
    emitted_system = False

    for raw in src_p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        if obj.get("type") != "message":
            out.append(raw)
            continue
        msg = obj["message"]
        role = msg.get("role")

        if system_prompt and not emitted_system and role in ("user", "assistant"):
            out.append(json.dumps({
                "type": "message",
                "id": "system-" + str(obj.get("id", "root")),
                "parentId": None,
                "timestamp": obj.get("timestamp"),
                "message": {
                    "role": "system",
                    "content": [{"type": "text", "text": system_prompt}],
                    "timestamp": 0,
                },
            }, ensure_ascii=False))
            emitted_system = True

        if role == "assistant":
            new_content = []
            for block in msg.get("content", []):
                if block.get("type") != "toolCall":
                    new_content.append(block)
                    continue
                nb = convert_tool_call(block, stats, mode)
                if nb is None:
                    new_content.append(block)
                    continue
                new_content.append(nb)
                bridged[block["id"]] = nb["name"]
                out.append(json.dumps({
                    "type": "bridgePlan",
                    "id": "plan-" + block["id"],
                    "parentId": obj.get("id"),
                    "timestamp": obj.get("timestamp"),
                    "encoding": "python-bridge",
                    "tool": nb["name"],
                    "elisp": BRIDGE_ELISP.get(nb["name"]),
                    "summary": nb["name"] + " -> " + str(nb["arguments"].get("path")),
                }, ensure_ascii=False))
            obj = dict(obj, message=dict(msg, content=new_content))
            out.append(json.dumps(obj, ensure_ascii=False))
            continue

        if role == "toolResult":
            tid = msg.get("toolCallId")
            if tid in bridged:
                stats.results_rewritten += 1
                orig = ""
                for c in msg.get("content", []):
                    if c.get("type") == "text":
                        orig = c.get("text", "")
                        break
                msg = dict(
                    msg,
                    toolName=bridged[tid],
                    content=[{
                        "type": "text",
                        "text": ";; bridge returned sexp\n(status ok) ;; source: " + orig,
                    }],
                    bridgeRewritten=True,
                )
                obj = dict(obj, message=msg)
            out.append(json.dumps(obj, ensure_ascii=False))
            continue

        out.append(json.dumps(obj, ensure_ascii=False))

    dst_p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ox-Alpha pi-trace -> Emacs python-bridge converter")
    p.add_argument("src", type=pathlib.Path)
    p.add_argument("dst", type=pathlib.Path)
    p.add_argument("--system-prompt", type=pathlib.Path)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--mode", choices=("ipython", "tools"), default="ipython",
                   help="ipython: rewrite every tool call as a prime-agent ipython REPL cell "
                        "(bash calls become await bash(...)); tools: keep named tool calls "
                        "(ox_bridge_write/ox_bridge_edit) with bridge_source attached)")
    ns = p.parse_args(argv)

    sp = ns.system_prompt.read_text(encoding="utf-8") if ns.system_prompt else None
    ns.dst.mkdir(parents=True, exist_ok=True)
    files = sorted(ns.src.glob("*.jsonl"))
    if ns.limit:
        files = files[: ns.limit]

    totals = ConvertStats()
    for i, f in enumerate(files, 1):
        s = convert_trace(f, ns.dst / f.name, sp, ns.mode)
        totals.wrote += s.wrote
        totals.edited += s.edited
        totals.bash_passthrough += s.bash_passthrough
        totals.results_rewritten += s.results_rewritten
        if i % 250 == 0 or i == len(files):
            print(f"[{i}/{len(files)}] write={totals.wrote} edit={totals.edited} "
                  f"bash={totals.bash_passthrough} results={totals.results_rewritten}")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
