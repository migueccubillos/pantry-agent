"""Dinner-planning agent.

Connects to the pantry MCP server over stdio, merges its tools with local
tools (recipe search, shopping-list writer) and runs a Claude tool-use loop.

Usage:
    python -m agent.agent "plan 3 dinners" [--diet vegetarian] [--avoid mushrooms] [--reset]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.local_tools import LOCAL_TOOLS, ToolError
from agent.tracing import Tracer

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server" / "inventory_server.py"
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
MAX_TURNS = 40
MAX_CONSECUTIVE_ERRORS = 6
MAX_DINNERS = 7

SYSTEM_PROMPT = """You are a home-cooking planning agent. You plan a week of dinners using what is
already in the pantry, buy only what is missing, and keep the pantry inventory accurate.

Tools:
- MCP pantry server: list_inventory, check_quantity, add_item, deduct_item (units: g, ml, unit).
- search_recipes / get_recipe: recipe catalogue. Recipe quantities already use g, ml, unit.
- write_shopping_list: writes the shopping list file.

Follow this procedure:
1. list_inventory to see the pantry.
2. search_recipes respecting every dietary constraint and avoided ingredient. Choose exactly N
   DIFFERENT recipes (one per dinner), preferring ones that reuse pantry items so the list stays short.
   Call get_recipe for each chosen recipe.
3. Aggregate the ingredient quantities across ALL chosen recipes (same ingredient in several
   recipes => sum). Missing = max(0, total_needed - in_pantry). Use the pantry's exact item names.
4. write_shopping_list with ONLY the missing quantities (once). If nothing is missing, skip it.
5. Simulate the shopping trip: add_item for every shopping-list item.
6. Simulate cooking the week: deduct_item for every ingredient of every chosen recipe.
7. list_inventory to confirm the final state.
8. Reply with the final answer (no more tool calls) in Markdown with sections:
   "Menu" (dinner number, recipe name, time, short steps), "Shopping list", "Inventory after the week".

Error handling: if a tool returns an error, read it and fix the cause (broaden the search, correct
the unit or name, re-check quantities) instead of repeating the same call. If a constraint is
impossible to satisfy, explain why in the final answer. You may call several independent tools in
parallel in one turn. Do not ask the user questions; this run is non-interactive."""


# ---------------------------------------------------------------- helpers
def parse_request(request: str) -> int:
    m = re.search(r"\b(\d+)\b", request)
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7}
    n = int(m.group(1)) if m else next((v for k, v in words.items() if re.search(rf"\b{k}\b", request.lower())), None)
    if n is None:
        raise SystemExit("Could not find how many dinners to plan. Example: \"plan 4 dinners\".")
    if not 1 <= n <= MAX_DINNERS:
        raise SystemExit(f"Please ask for between 1 and {MAX_DINNERS} dinners (got {n}).")
    return n


def mcp_result_to_python(result) -> tuple[object, bool]:
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        joined = "\n".join(texts)
        try:
            payload = json.loads(joined)
        except (json.JSONDecodeError, TypeError):
            payload = joined
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    if result.isError:
        payload = "\n".join(texts) or str(payload)
    return payload, bool(result.isError)


# ---------------------------------------------------------------- main loop
async def run(request: str, diet: list[str], avoid: list[str], model: str, reset: bool, run_name: str) -> int:
    n = parse_request(request)
    db_path = ROOT / "data" / "inventory.json"
    if reset or not db_path.exists():
        shutil.copy(ROOT / "data" / "inventory.seed.json", db_path)

    client = Anthropic()
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={**os.environ, "PANTRY_DB": str(db_path)},
    )

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        mcp_tools = (await session.list_tools()).tools
        mcp_names = {t.name for t in mcp_tools}

        tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in mcp_tools
        ] + [
            {"name": name, "description": spec["description"], "input_schema": spec["input_schema"]}
            for name, spec in LOCAL_TOOLS.items()
        ]

        async def call_tool(name: str, args: dict) -> tuple[object, bool, str]:
            if name in mcp_names:
                res = await session.call_tool(name, args)
                out, err = mcp_result_to_python(res)
                return out, err, "mcp"
            if name in LOCAL_TOOLS:
                try:
                    return LOCAL_TOOLS[name]["fn"](**args), False, "local"
                except ToolError as e:
                    return str(e), True, "local"
                except TypeError as e:
                    return f"Bad arguments for {name}: {e}", True, "local"
            return f"Unknown tool '{name}'.", True, "none"

        initial_inv, _ = mcp_result_to_python(await session.call_tool("list_inventory", {}))
        tracer = Tracer(ROOT / "traces", run_name)
        tracer.log(
            "run_start", run_name=run_name, request=request, dinners=n, diet=diet, avoid=avoid,
            model=model, mcp_tools=sorted(mcp_names), local_tools=list(LOCAL_TOOLS),
            initial_inventory=initial_inv,
        )

        user_msg = f"Request: {request}\nNumber of dinners (N): {n}\n"
        user_msg += f"Dietary constraints: {', '.join(diet) if diet else 'none'}\n"
        user_msg += f"Ingredients to avoid: {', '.join(avoid) if avoid else 'none'}\n"
        messages: list[dict] = [{"role": "user", "content": user_msg}]

        totals = {"input_tokens": 0, "output_tokens": 0}
        tool_calls = tool_errors = consecutive_errors = 0
        shopping_list = None
        final_answer, status = "", "max_turns_reached"
        turn = 0

        for turn in range(1, MAX_TURNS + 1):
            resp = client.messages.create(
                model=model, max_tokens=8000, system=SYSTEM_PROMPT, tools=tools, messages=messages,
            )
            totals["input_tokens"] += resp.usage.input_tokens
            totals["output_tokens"] += resp.usage.output_tokens
            text = "\n".join(b.text for b in resp.content if b.type == "text")
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            tracer.log(
                "llm_response", turn=turn, stop_reason=resp.stop_reason, text=text,
                tool_uses=[{"name": b.name, "input": b.input} for b in tool_uses],
                usage={"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
            )
            print(f"[turn {turn}] stop={resp.stop_reason} tools={[b.name for b in tool_uses]}", flush=True)

            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                final_answer = text
                status = "completed" if resp.stop_reason == "end_turn" else f"stopped:{resp.stop_reason}"
                break

            results = []
            for b in tool_uses:
                t0 = time.perf_counter()
                out, is_err, source = await call_tool(b.name, b.input)
                ms = int((time.perf_counter() - t0) * 1000)
                tool_calls += 1
                tool_errors += is_err
                consecutive_errors = consecutive_errors + 1 if is_err else 0
                if b.name == "write_shopping_list" and not is_err:
                    shopping_list = out["saved_to"]
                tracer.log("tool_call", turn=turn, name=b.name, source=source, input=b.input,
                           output=out, is_error=is_err, ms=ms)
                results.append({
                    "type": "tool_result", "tool_use_id": b.id, "is_error": is_err,
                    "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": results})

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                msg = (f"{consecutive_errors} consecutive tool errors. Stop calling tools and give the "
                       "final answer explaining what could not be completed.")
                tracer.log("guardrail", message=msg)
                messages.append({"role": "user", "content": msg})
                consecutive_errors = 0

        final_inv, _ = mcp_result_to_python(await session.call_tool("list_inventory", {}))
        tracer.log(
            "run_end", status=status, turns=turn, tool_calls=tool_calls, tool_errors=tool_errors,
            usage_total=totals, shopping_list=shopping_list, final_answer=final_answer,
            final_inventory=final_inv,
        )
        tracer.close()

    print("\n" + "=" * 70 + "\n" + final_answer + "\n" + "=" * 70)
    print(f"Status: {status} | turns={turn} | tool calls={tool_calls} (errors={tool_errors})")
    print(f"Trace: {tracer.md_path.relative_to(ROOT)}  |  {tracer.jsonl_path.relative_to(ROOT)}")
    return 0 if status == "completed" else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Plan N dinners with a pantry-aware agent.")
    p.add_argument("request", help='e.g. "plan 3 dinners"')
    p.add_argument("--diet", action="append", default=[], help="Dietary tag (repeatable): vegetarian, vegan, gluten-free, dairy-free, pescatarian, high-protein")
    p.add_argument("--avoid", action="append", default=[], help="Ingredient to avoid (repeatable)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reset", action="store_true", help="Reset pantry to data/inventory.seed.json first")
    p.add_argument("--run-name", default=None)
    a = p.parse_args()
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first (see README).")
    run_name = a.run_name or "run_" + datetime.now().strftime("%Y%m%d-%H%M%S")
    sys.exit(asyncio.run(run(a.request, a.diet, a.avoid, a.model, a.reset, run_name)))


if __name__ == "__main__":
    main()
