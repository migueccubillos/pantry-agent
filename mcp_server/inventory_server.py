"""Pantry inventory MCP server.

Exposes four tools over MCP (stdio transport):
    list_inventory, add_item, deduct_item, check_quantity

State is persisted in a JSON file (env PANTRY_DB, default data/inventory.json).
Quantities are stored in a canonical base unit per item:
    mass   -> g   (accepts g, kg)
    volume -> ml  (accepts ml, l)
    count  -> unit (accepts unit, units, pcs, pc)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PANTRY_DB", ROOT / "data" / "inventory.json"))

# unit -> (dimension, factor to base unit)
UNITS: dict[str, tuple[str, float]] = {
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "ml": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "unit": ("count", 1.0),
    "units": ("count", 1.0),
    "pcs": ("count", 1.0),
    "pc": ("count", 1.0),
}
BASE_UNIT = {"mass": "g", "volume": "ml", "count": "unit"}

_lock = threading.Lock()
mcp = FastMCP("pantry-inventory", log_level="WARNING")


# ---------------------------------------------------------------- helpers
def _key(name: str) -> str:
    key = " ".join(name.strip().lower().split())
    if not key:
        raise ValueError("Item name must not be empty.")
    return key


def _to_base(quantity: float, unit: str) -> tuple[float, str]:
    u = unit.strip().lower()
    if u not in UNITS:
        raise ValueError(
            f"Unknown unit '{unit}'. Supported units: {', '.join(sorted(UNITS))}."
        )
    if quantity is None or quantity <= 0:
        raise ValueError(f"Quantity must be a positive number (got {quantity}).")
    dim, factor = UNITS[u]
    return round(quantity * factor, 3), BASE_UNIT[dim]


def _load() -> dict:
    if not DB_PATH.exists():
        return {}
    return json.loads(DB_PATH.read_text(encoding="utf-8"))


def _save(inv: dict) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(inv, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(DB_PATH)


def _fmt(q: float) -> float | int:
    return int(q) if float(q).is_integer() else round(q, 3)


# ---------------------------------------------------------------- tools
@mcp.tool()
def list_inventory() -> dict:
    """List every item currently in the pantry with its quantity and base unit
    (g, ml or unit). Items with zero quantity are omitted."""
    inv = _load()
    items = [
        {"name": k, "quantity": _fmt(v["quantity"]), "unit": v["unit"]}
        for k, v in sorted(inv.items())
        if v["quantity"] > 0
    ]
    return {"count": len(items), "items": items}


@mcp.tool()
def add_item(name: str, quantity: float, unit: str) -> dict:
    """Add a quantity of an item to the pantry (creates it if missing).
    unit must be one of: g, kg, ml, l, unit. kg/l are converted to g/ml."""
    key = _key(name)
    qty, base = _to_base(quantity, unit)
    with _lock:
        inv = _load()
        if key in inv and inv[key]["unit"] != base:
            raise ValueError(
                f"'{key}' is tracked in '{inv[key]['unit']}', cannot add '{unit}'."
            )
        current = inv.get(key, {"quantity": 0.0, "unit": base})
        current["quantity"] = round(current["quantity"] + qty, 3)
        inv[key] = current
        _save(inv)
    return {"name": key, "added": _fmt(qty), "quantity": _fmt(current["quantity"]), "unit": base}


@mcp.tool()
def deduct_item(name: str, quantity: float, unit: str) -> dict:
    """Remove a quantity of an item from the pantry (e.g. after cooking).
    Fails if the item does not exist, the unit is incompatible, or there is
    not enough stock — nothing is changed in that case."""
    key = _key(name)
    qty, base = _to_base(quantity, unit)
    with _lock:
        inv = _load()
        if key not in inv or inv[key]["quantity"] <= 0:
            raise ValueError(f"'{key}' is not in the pantry.")
        if inv[key]["unit"] != base:
            raise ValueError(
                f"'{key}' is tracked in '{inv[key]['unit']}', cannot deduct '{unit}'."
            )
        have = inv[key]["quantity"]
        if qty > have + 1e-9:
            raise ValueError(
                f"Not enough '{key}': have {_fmt(have)} {base}, tried to deduct {_fmt(qty)} {base}."
            )
        inv[key]["quantity"] = round(have - qty, 3)
        _save(inv)
    return {"name": key, "deducted": _fmt(qty), "remaining": _fmt(inv[key]["quantity"]), "unit": base}


@mcp.tool()
def check_quantity(name: str) -> dict:
    """Return how much of an item is in the pantry (0 if absent)."""
    key = _key(name)
    inv = _load()
    item = inv.get(key)
    if not item:
        return {"name": key, "in_stock": False, "quantity": 0, "unit": None}
    return {
        "name": key,
        "in_stock": item["quantity"] > 0,
        "quantity": _fmt(item["quantity"]),
        "unit": item["unit"],
    }


if __name__ == "__main__":
    mcp.run()  # stdio transport
