"""Offline tests (no API key needed): MCP server over real stdio + local tools."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.agent import mcp_result_to_python, parse_request
from agent.local_tools import ToolError, get_recipe, search_recipes, write_shopping_list

ROOT = Path(__file__).resolve().parent.parent


async def _call_many(db: Path, calls):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server" / "inventory_server.py")],
        env={**os.environ, "PANTRY_DB": str(db)},
    )
    out = []
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"list_inventory", "add_item", "deduct_item", "check_quantity"} <= names
        for name, args in calls:
            out.append(mcp_result_to_python(await s.call_tool(name, args)))
    return out


def test_mcp_inventory_roundtrip(tmp_path):
    db = tmp_path / "inv.json"
    shutil.copy(ROOT / "data" / "inventory.seed.json", db)
    res = asyncio.run(_call_many(db, [
        ("check_quantity", {"name": "  Olive OIL "}),
        ("deduct_item", {"name": "olive oil", "quantity": 0.1, "unit": "l"}),
        ("add_item", {"name": "Lemon", "quantity": 2, "unit": "unit"}),
        ("deduct_item", {"name": "egg", "quantity": 99, "unit": "unit"}),   # not enough
        ("add_item", {"name": "rice", "quantity": 1, "unit": "cups"}),      # bad unit
        ("deduct_item", {"name": "truffle", "quantity": 1, "unit": "g"}),   # missing item
        ("check_quantity", {"name": "egg"}),
    ]))
    assert res[0][0]["quantity"] == 500 and not res[0][1]
    assert res[1][0]["remaining"] == 400
    assert res[2][0]["quantity"] == 2
    assert res[3][1] and "Not enough" in res[3][0]
    assert res[4][1] and "Unknown unit" in res[4][0]
    assert res[5][1] and "not in the pantry" in res[5][0]
    assert res[6][0]["quantity"] == 6  # failed deduct changed nothing
    saved = json.loads(db.read_text())
    assert saved["lemon"] == {"quantity": 2, "unit": "unit"}


def test_search_filters_and_errors():
    veg = search_recipes(diet=["vegetarian", "gluten-free"], exclude_ingredients=["mushroom"], max_results=10)
    ids = {r["id"] for r in veg["results"]}
    assert ids == {"r02", "r05", "r08", "r12"}
    with pytest.raises(ToolError):
        search_recipes(diet=["keto"])
    with pytest.raises(ToolError):
        search_recipes(query="sushi")
    with pytest.raises(ToolError):
        get_recipe("r99")


def test_shopping_list_validation(tmp_path, monkeypatch):
    import agent.local_tools as lt
    monkeypatch.setattr(lt, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(lt, "ROOT", tmp_path)
    out = write_shopping_list([{"name": "Basil", "quantity": 10, "unit": "g", "for": "pasta"}])
    assert out["item_count"] == 1 and (tmp_path / Path(out["saved_to"]).name).exists()
    with pytest.raises(ToolError):
        write_shopping_list([])
    with pytest.raises(ToolError):
        write_shopping_list([{"name": "x", "quantity": -1, "unit": "g"}])


def test_parse_request():
    assert parse_request("plan 4 dinners") == 4
    assert parse_request("plan three dinners") == 3
    with pytest.raises(SystemExit):
        parse_request("plan dinners")
    with pytest.raises(SystemExit):
        parse_request("plan 30 dinners")
