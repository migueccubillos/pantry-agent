"""Minimal tracer: every event goes to a JSONL file; a readable Markdown trace
is rendered at the end of the run."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


class Tracer:
    def __init__(self, trace_dir: Path, run_name: str):
        trace_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = trace_dir / f"{run_name}.jsonl"
        self.md_path = trace_dir / f"{run_name}.md"
        self.events: list[dict] = []
        self.t0 = time.perf_counter()
        self._fh = self.jsonl_path.open("w", encoding="utf-8")

    def log(self, kind: str, **data) -> None:
        ev = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_s": round(time.perf_counter() - self.t0, 3),
            "kind": kind,
            **data,
        }
        self.events.append(ev)
        self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._fh.flush()

    # ------------------------------------------------------------ markdown
    def close(self) -> None:
        self._fh.close()
        self.md_path.write_text(self._render(), encoding="utf-8")

    def _render(self) -> str:
        out: list[str] = []
        for ev in self.events:
            k = ev["kind"]
            t = f"`+{ev['elapsed_s']:.1f}s`"
            if k == "run_start":
                out += [
                    f"# Trace — {ev['run_name']}",
                    "",
                    f"- **Request:** {ev['request']}",
                    f"- **Diet:** {ev.get('diet') or 'none'}  ",
                    f"- **Model:** `{ev['model']}`",
                    f"- **Started:** {ev['ts']}",
                    f"- **Tools:** MCP → {', '.join(ev['mcp_tools'])}; local → {', '.join(ev['local_tools'])}",
                    "",
                    "## Initial inventory",
                    "",
                    _inv_table(ev["initial_inventory"]),
                    "",
                    "## Agent loop",
                    "",
                ]
            elif k == "llm_response":
                u = ev["usage"]
                out.append(
                    f"### Turn {ev['turn']} {t} — stop_reason=`{ev['stop_reason']}` "
                    f"(in {u['input_tokens']} / out {u['output_tokens']} tokens)"
                )
                if ev.get("text"):
                    out += ["", "> " + ev["text"].strip().replace("\n", "\n> "), ""]
            elif k == "tool_call":
                flag = "❌ ERROR" if ev["is_error"] else "✅"
                out += [
                    f"- {flag} **{ev['source']}::{ev['name']}** ({ev['ms']} ms)",
                    f"  - input: `{json.dumps(ev['input'], ensure_ascii=False)}`",
                    f"  - output: `{_short(ev['output'])}`",
                ]
            elif k == "guardrail":
                out += ["", f"> ⚠️ **Guardrail:** {ev['message']}", ""]
            elif k == "run_end":
                u = ev["usage_total"]
                out += [
                    "",
                    "## Result",
                    "",
                    f"- **Status:** {ev['status']}",
                    f"- **Turns:** {ev['turns']} · **Tool calls:** {ev['tool_calls']} "
                    f"(errors recovered: {ev['tool_errors']})",
                    f"- **Tokens:** in {u['input_tokens']} / out {u['output_tokens']}",
                    f"- **Duration:** {ev['elapsed_s']:.1f}s",
                    f"- **Shopping list file:** {ev.get('shopping_list') or '—'}",
                    "",
                    "## Final answer",
                    "",
                    ev.get("final_answer", ""),
                    "",
                    "## Inventory after the week (verified directly against the MCP server)",
                    "",
                    _inv_table(ev["final_inventory"]),
                    "",
                ]
        return "\n".join(out)


def _short(obj, limit: int = 400) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    s = s.replace("`", "'").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + " …"


def _inv_table(inv: dict) -> str:
    rows = ["| Item | Qty | Unit |", "|---|---:|---|"]
    rows += [f"| {i['name']} | {i['quantity']} | {i['unit']} |" for i in inv.get("items", [])]
    return "\n".join(rows)
