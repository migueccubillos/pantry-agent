"""Agent-side (non-MCP) tools: mocked recipe search and a shopping-list writer.

Each tool is a plain Python function plus a JSON schema the LLM sees.
Errors are raised as ToolError so the agent loop can return them to the model
with is_error=True and let it replan.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES: list[dict] = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
OUTPUT_DIR = ROOT / "output"

KNOWN_DIETS = sorted({t for r in RECIPES for t in r["tags"]})


class ToolError(Exception):
    """Recoverable tool failure — surfaced to the model as an error result."""


# ---------------------------------------------------------------- recipe search (mocked web search)
def search_recipes(
    query: str = "",
    diet: list[str] | None = None,
    exclude_ingredients: list[str] | None = None,
    max_results: int = 6,
) -> dict:
    diet = [d.strip().lower() for d in (diet or []) if d.strip()]
    exclude = [e.strip().lower() for e in (exclude_ingredients or []) if e.strip()]
    unknown = [d for d in diet if d not in KNOWN_DIETS]
    if unknown:
        raise ToolError(
            f"Unknown diet tag(s): {unknown}. Valid tags: {KNOWN_DIETS}."
        )
    terms = [t for t in query.lower().split() if t]

    hits = []
    for r in RECIPES:
        if any(d not in r["tags"] for d in diet):
            continue
        ing_names = [i["name"] for i in r["ingredients"]]
        if any(ex in name for ex in exclude for name in ing_names):
            continue
        haystack = " ".join([r["name"], r["cuisine"], *r["tags"], *ing_names]).lower()
        score = sum(t in haystack for t in terms) if terms else 1
        if terms and score == 0:
            continue
        hits.append((score, r))

    hits.sort(key=lambda x: (-x[0], x[1]["id"]))
    results = [
        {
            "id": r["id"],
            "name": r["name"],
            "servings": r["servings"],
            "minutes": r["minutes"],
            "tags": r["tags"],
            "main_ingredients": [i["name"] for i in r["ingredients"]],
        }
        for _, r in hits[: max(1, min(max_results, 10))]
    ]
    if not results:
        raise ToolError(
            "No recipes matched. Try a broader query (or an empty query), fewer diet tags, "
            "or fewer exclusions."
        )
    return {"count": len(results), "results": results}


def get_recipe(recipe_id: str) -> dict:
    for r in RECIPES:
        if r["id"] == recipe_id.strip().lower():
            return r
    raise ToolError(f"Recipe '{recipe_id}' not found. Use an id returned by search_recipes.")


# ---------------------------------------------------------------- shopping-list writer
def write_shopping_list(items: list[dict], title: str = "Shopping list") -> dict:
    if not items:
        raise ToolError("items is empty. If nothing is missing, say so in the final answer instead.")
    clean = []
    for it in items:
        name = str(it.get("name", "")).strip().lower()
        qty = it.get("quantity")
        unit = str(it.get("unit", "")).strip().lower()
        if not name or not isinstance(qty, (int, float)) or qty <= 0 or not unit:
            raise ToolError(f"Invalid shopping-list item: {it}. Need name, positive quantity, unit.")
        clean.append({"name": name, "quantity": qty, "unit": unit, "for": it.get("for", "")})

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUTPUT_DIR / f"shopping_list_{stamp}.md"
    lines = [f"# {title}", "", "| Item | Quantity | Unit | Needed for |", "|---|---:|---|---|"]
    lines += [f"| {c['name']} | {c['quantity']} | {c['unit']} | {c['for']} |" for c in clean]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"saved_to": str(path.relative_to(ROOT)), "item_count": len(clean), "items": clean}


# ---------------------------------------------------------------- schemas for the LLM
LOCAL_TOOLS = {
    "search_recipes": {
        "fn": search_recipes,
        "description": (
            "Search the recipe catalogue (mock web search). Returns recipe summaries; "
            "call get_recipe for exact ingredient quantities. Raises an error when nothing matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text keywords (cuisine, ingredient, dish). Empty = everything."},
                "diet": {"type": "array", "items": {"type": "string"}, "description": f"Required tags, any of {KNOWN_DIETS}."},
                "exclude_ingredients": {"type": "array", "items": {"type": "string"}, "description": "Ingredient substrings to avoid."},
                "max_results": {"type": "integer", "default": 6},
            },
        },
    },
    "get_recipe": {
        "fn": get_recipe,
        "description": "Get the full recipe (ingredients with quantities in g/ml/unit, steps) by id.",
        "input_schema": {
            "type": "object",
            "properties": {"recipe_id": {"type": "string"}},
            "required": ["recipe_id"],
        },
    },
    "write_shopping_list": {
        "fn": write_shopping_list,
        "description": (
            "Write the shopping list of ONLY the missing quantities to a markdown file. "
            "Call exactly once, after comparing aggregated recipe needs with the pantry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string", "enum": ["g", "ml", "unit"]},
                            "for": {"type": "string", "description": "Recipe(s) needing it."},
                        },
                        "required": ["name", "quantity", "unit"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}
