# ox-alpha-pi-traces-emacs-bridge — converter

Converts the agent traces from
[TeichAI/Ox-Alpha-Pi-Traces](https://huggingface.co/datasets/TeichAI/Ox-Alpha-Pi-Traces)
so every `write` / `edit` tool call runs **inside Emacs** through
[python-bridge](https://github.com/manateelazycat/python-bridge).

The already-converted dataset lives on Hugging Face:
**[Jadson/ox-alpha-pi-traces-emacs-bridge](https://huggingface.co/datasets/Jadson/ox-alpha-pi-traces-emacs-bridge)**

## Usage

```bash
# prime-agent REPL cells (default)
python convert_traces.py traces/ out_ipython/ --system-prompt system_prompt.md

# named tool calls with bridge_source attached
python convert_traces.py traces/ out_tools/ --system-prompt system_prompt.md --mode tools
```

`--system-prompt` is optional; pass the file you want injected as the first
`message` event of each converted trace.

## What the converter does

- `write(path, content)` → a cell that calls the registered Emacs method
  `ox-bridge-write` via `get_emacs_func_result`.
- `edit(path, [{oldText, newText}, ...])` → a cell that normalizes the hunk
  keys to `old`/`new`, serializes them as JSON, and calls
  `ox-bridge-edit` via `get_emacs_func_result`.
- `bash(cmd)` → `await bash(cmd)` in `ipython` mode, untouched in `tools`
  mode.
- Malformed calls (missing `path`, hunks that aren't `{old,new}` shape)
  are left unchanged so a re-run never corrupts data.

`eval_in_emacs` is never emitted: it is fire-and-forget and always returns
`nil`. The converter only emits synchronous EPC calls.

## Dataset finalization

This repository also publishes the deterministic curation snapshot in
`final_v1/`. Rebuild it from the pipeline workspace with:

```bash
python finalize_dataset.py --root dataset_build --output dataset_build/final_v1
```

The snapshot contains `manifest.json`, `schema.json`, `quality_report.json`,
curation labels, explicit-only goal review, negative-example candidates,
lexical leakage review, reviewed queue decisions, splits, and
`checksums.sha256`. It covers 2,246 traces: 2,212 valid and 34 quarantined.
The current freeze ID is
`afe9aef614d24268245c7970f68eac5dfac1dd9296d606cf82d06ffb83703266`.

### Curation Stats

- **Outcomes**: 2118 success, 39 partial_success, 17 inconclusive, 38 failure, 34 quarantined
- **Difficulty**: 792 easy, 1197 medium, 257 hard
- **Negative examples**: 339

The snapshot is reproducible, but semantic labels remain candidates. The
manifest and quality report keep `human_approval_required: true`; raw traces
are never modified by finalization.

## Emacs side

The traces call methods that must be registered in Emacs with
`epc-define-method`. A ready implementation (`ox-bridge-write` /
`ox-bridge-edit`) ships inside the Hugging Face dataset repo as
`ox_trace_bridge.el`.
