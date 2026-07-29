"""MCP server for Cronometer nutrition data via the mobile REST API."""

import json
import logging
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from .client import CronometerClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "cronometer",
    instructions=(
        "Cronometer MCP server for nutrition tracking via the mobile REST API. "
        "Provides access to food search, diary management, daily nutrition data, "
        "macro targets, biometrics, and fasting history from Cronometer. "
        "Use search_foods to find foods, get_food_details for nutrition info "
        "and serving sizes, add_food_entry to log meals, and get_food_log to "
        "review what was eaten."
    ),
)

_client: CronometerClient | None = None


def _get_client() -> CronometerClient:
    global _client
    if _client is None:
        _client = CronometerClient()
    return _client


def _parse_date(d: str | None) -> date | None:
    if d is None:
        return None
    return date.fromisoformat(d)


def _ok(data: dict) -> str:
    """Wrap a successful response."""
    return json.dumps({"status": "success", **data}, indent=2)


def _err(e: Exception) -> str:
    """Wrap an error response with actionable messages."""
    import httpx

    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401 or status == 403:
            msg = "Authentication failed. Cronometer session may have expired -- try again."
        elif status == 429:
            msg = "Rate limit exceeded. Wait a few minutes before retrying."
        elif status == 404:
            msg = f"Resource not found (HTTP {status})."
        else:
            msg = f"Cronometer API error (HTTP {status})."
    elif isinstance(e, httpx.TimeoutException):
        msg = "Request timed out. Cronometer may be slow -- try again."
    elif isinstance(e, httpx.ConnectError):
        msg = "Could not connect to Cronometer. Check network connectivity."
    else:
        msg = f"{type(e).__name__}: {e}"

    return json.dumps({"status": "error", "message": msg})


# ------------------------------------------------------------------
# Diary: read
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_food_log(date: str | None = None) -> str:
    """Get all diary entries for a given date.

    Returns every food entry logged for the day. Each "Serving" entry is
    enriched (best-effort) with the food's name, source, the serving measure
    (unit name and grams per unit), the number of servings, and that food's
    own nutrient profile scaled to the amount eaten. Non-food entries
    (exercise, biometrics) carry their own name.

    Note: the per-entry "nutrients" are each food's individual contribution,
    which is distinct from the day-level nutrition_summary aggregate below.

    Also returns a top-level energy_summary field with pre-computed
    values most relevant to the user:

      - total_target_kcal: daily calorie target dynamically adjusted
        for expenditure and weight goal (equivalent to Cronometer's
        "Total Target" in the Energy Summary screen)
      - consumed_kcal: total calories consumed
      - remaining_kcal: calories remaining to stay on target
        (total_target_kcal - consumed_kcal). Always report this
        when summarizing the user's day. Prefer this over manually
        deriving values from the burn breakdown fields.

    Also returns a nutrition_summary field with consumed totals for every
    nutrient the user tracks in Cronometer (macros plus any tracked
    micronutrients such as saturated fat, cholesterol, or omega-3/6):

      - macros: flat macro totals (energy, protein, carbs, net_carbs, fat,
        fiber, alcohol)
      - nutrients: the full list of tracked nutrients with amounts and units

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_diary(day)
        data = client.enrich_diary_servings(data)

        summary = (data or {}).get("summary") or {}
        target = (summary.get("macros") or {}).get("energy")
        consumed = (summary.get("consumed") or {}).get("total")
        energy_summary: dict | None = None
        if target is not None and consumed is not None:
            energy_summary = {
                "total_target_kcal": target,
                "consumed_kcal": consumed,
                "remaining_kcal": int(round(target - consumed)),
            }

        nutrition_summary = client.get_consumed_nutrients(day)

        return _ok(
            {
                "date": date or str(date_module_today()),
                "energy_summary": energy_summary,
                "nutrition_summary": nutrition_summary,
                "diary": data,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: write
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def add_food_entry(
    food_id: int,
    measure_id: int,
    grams: float,
    date: str | None = None,
    translation_id: int = 0,
    diary_group: str = "auto",
) -> str:
    """Add a food entry to the Cronometer diary.

    Use search_foods to find food_id and measure_id, then
    get_food_details to confirm serving sizes and gram weights.

    Args:
        food_id: Numeric food ID from search_foods results.
        measure_id: Measure/unit ID from get_food_details.
        grams: Weight of the serving in grams.
        date: Date to log as YYYY-MM-DD (defaults to today).
        translation_id: Translation ID from search results (usually 0).
        diary_group: Meal slot -- one of "auto", "breakfast", "lunch",
                     "dinner", "snacks" (case-insensitive, default "auto").
    """
    try:
        group_map = {
            "auto": 0,
            "breakfast": 1,
            "lunch": 2,
            "dinner": 3,
            "snacks": 4,
        }
        group_key = diary_group.strip().lower()
        group_int = group_map.get(group_key)
        if group_int is None:
            return _err(
                ValueError(
                    f"Invalid diary_group '{diary_group}'. "
                    "Must be one of: auto, breakfast, lunch, dinner, snacks."
                )
            )

        client = _get_client()
        day = _parse_date(date)
        result = client.add_serving(
            food_id=food_id,
            measure_id=measure_id,
            grams=grams,
            translation_id=translation_id,
            day=day,
            diary_group=group_int,
        )
        return _ok(
            {
                "entry": result,
                "note": "Use the serving ID to remove this entry with remove_food_entry.",
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def remove_food_entry(
    entry_ids: list[str],
    date: str | None = None,
) -> str:
    """Remove one or more food entries from the Cronometer diary.

    Use get_food_log to find entry IDs.

    Args:
        entry_ids: List of serving/entry IDs to remove.
        date: Date the entries belong to as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        result = client.delete_entries(entry_ids, day)
        return _ok(
            {
                "removed": result.get("removed", []),
                "count": result.get("count", 0),
                "date": date or str(date_module_today()),
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Diary: management
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def mark_day_complete(date: str, complete: bool = True) -> str:
    """Mark a diary day as complete or incomplete.

    Args:
        date: Date to mark as YYYY-MM-DD.
        complete: True to mark complete, False for incomplete.
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        result = client.mark_day_complete(day, complete)
        status = "complete" if complete else "incomplete"
        return _ok(
            {
                "date": date,
                "marked": status,
                "result": result,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def copy_day(date: str | None = None) -> str:
    """Copy all diary entries from the previous day to the given date.

    Additive -- does not remove existing entries on the destination date.

    Args:
        date: Destination date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        result = client.copy_day(to_day=day)
        return _ok(
            {
                "destination_date": date or str(date_module_today()),
                "result": result,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Nutrition
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_daily_nutrition(date: str | None = None) -> str:
    """Get daily nutrition summary with consumed macro and micronutrient totals.

    Returns the amounts actually consumed for the day, covering every nutrient
    the user tracks in Cronometer (i.e. has a target set for). The response has:

      - summary: flat macro totals (energy, protein, carbs, net_carbs, fat,
        fiber, alcohol). A value is null if that macro isn't tracked.
      - nutrients: the full list of tracked nutrients, each with id, name,
        amount, unit, category, and confidence.

    A nutrient only appears if it's tracked in Cronometer. To surface e.g.
    saturated fat, cholesterol, or trans fat, set a target for it in Cronometer
    and it will flow through automatically.

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_consumed_nutrients(day)
        return _ok(
            {
                "date": date or str(date_module_today()),
                "summary": data["macros"],
                "nutrients": data["nutrients"],
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_nutrition_scores(date: str | None = None) -> str:
    """Get nutrition scores with per-nutrient consumed amounts and category grades.

    Returns category scores (All Targets, Vitamins, Minerals, Electrolytes,
    Antioxidants, Immune Support, Metabolism, Bone Health) with the actual
    consumed amount and confidence level for each tracked nutrient.

    This is the richest nutrition endpoint -- use it when you need to know
    both how much of each nutrient was consumed AND how close each is to
    the target.

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_nutrition_scores(day)
        return _ok(
            {
                "date": date or str(date_module_today()),
                "scores": data,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Food search and details
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def search_foods(query: str) -> str:
    """Search Cronometer's food database by name.

    Returns matching foods with their IDs and source information.
    Use the food_id and measure_id from results with add_food_entry,
    or pass food_id to get_food_details for full nutrition info.

    Args:
        query: Food name or keyword (e.g. "eggs", "chicken breast").
    """
    try:
        client = _get_client()
        foods = client.search_food(query)

        # Slim down results to the most useful fields
        results = []
        for f in foods:
            results.append(
                {
                    "food_id": f.get("id"),
                    "name": f.get("name"),
                    "source": f.get("source"),
                    "measure_id": f.get("measureId"),
                    "translation_id": f.get("translationId"),
                    "measure_display": f.get("measureDisplayName"),
                    "score": f.get("score"),
                }
            )

        return _ok(
            {
                "query": query,
                "count": len(results),
                "foods": results,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_food_details(food_id: int) -> str:
    """Get detailed food information including nutrition and serving sizes.

    Use this after search_foods to get the full nutrient profile and
    available measure_ids needed for add_food_entry.

    Args:
        food_id: Food ID from search_foods results.
    """
    try:
        client = _get_client()
        data = client.get_food(food_id)

        # Extract measures for easy reference
        measures = []
        for m in data.get("measures", []):
            measures.append(
                {
                    "measure_id": m.get("id"),
                    "name": m.get("name"),
                    "grams": m.get("value"),
                }
            )

        return _ok(
            {
                "food_id": data.get("id"),
                "name": data.get("name"),
                "default_measure_id": data.get("defaultMeasureId"),
                "measures": measures,
                "nutrients": data.get("nutrients", []),
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Custom food creation
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def add_custom_food(
    name: str,
    calories: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    fiber_g: float = 0,
    sugar_g: float = 0,
    sodium_mg: float = 0,
    saturated_fat_g: float = 0,
    serving_name: str = "1 serving",
    serving_grams: float = 100.0,
) -> str:
    """Create a custom food in Cronometer with specified nutrition.

    Nutrient amounts should be for the full serving size specified.
    After creation, use the returned food_id with add_food_entry to log it.

    Args:
        name: Food name.
        calories: Calories per serving (kcal).
        protein_g: Protein per serving (g).
        fat_g: Fat per serving (g).
        carbs_g: Carbs per serving (g).
        fiber_g: Fiber per serving (g, default 0).
        sugar_g: Sugar per serving (g, default 0).
        sodium_mg: Sodium per serving (mg, default 0).
        saturated_fat_g: Saturated fat per serving (g, default 0).
        serving_name: Name for the serving size (default "1 serving").
        serving_grams: Weight of one serving in grams (default 100).
    """
    try:
        client = _get_client()
        result = client.create_custom_food(
            name,
            calories=calories,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            fiber_g=fiber_g,
            sugar_g=sugar_g,
            sodium_mg=sodium_mg,
            saturated_fat_g=saturated_fat_g,
            serving_name=serving_name,
            serving_grams=serving_grams,
        )

        # Fetch back to get the server-assigned measure_id
        food_data = client.get_food(result["food_id"])
        result["measure_id"] = food_data.get("defaultMeasureId")

        return _ok(
            {
                "food_id": result["food_id"],
                "measure_id": result["measure_id"],
                "name": name,
                "note": "Use food_id and measure_id with add_food_entry to log this food.",
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Macro targets
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_macro_targets() -> str:
    """Get current macro targets including weekly schedule and templates.

    Returns the weekly macro schedule (which template applies to each day)
    and all saved macro target templates with their values.
    """
    try:
        client = _get_client()
        schedules = client.get_macro_schedules()
        templates = client.get_macro_target_templates()
        return _ok(
            {
                "schedules": schedules,
                "templates": templates,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Fasting
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_fasting_history(
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get fasting history from Cronometer.

    Returns fasts within the date range including status, timestamps,
    and duration.

    Args:
        start_date: Start date as YYYY-MM-DD (defaults to 30 days ago).
        end_date: End date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        data = client.get_fasting_with_date_range(start, end)
        return _ok(
            {
                "start_date": start_date
                or str(date_module_today() - timedelta(days=30)),
                "end_date": end_date or str(date_module_today()),
                "fasting": data,
            }
        )
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_fasting_stats() -> str:
    """Get aggregate fasting statistics.

    Returns total fasting hours, longest fast, average fast duration,
    and completed fast count.
    """
    try:
        client = _get_client()
        data = client.get_fasting_stats()
        return _ok({"stats": data})
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Biometrics
# ------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def list_biometrics() -> str:
    """List the biometric metrics tracked in Cronometer.

    Returns every metric type the account can record (Weight, Body Fat,
    Heart Rate, Blood Glucose, Waist Size, Sleep, blood panels, body
    measurements, etc.). Use the metric_id and a unit_id from the results
    with get_biometrics.
    """
    try:
        client = _get_client()
        metrics = client.get_metrics()

        # Slim down results to the fields needed to call get_biometrics
        results = []
        for m in metrics:
            results.append(
                {
                    "metric_id": m.get("id"),
                    "name": m.get("name"),
                    "units": [
                        {"unit_id": u.get("id"), "name": u.get("name")}
                        for u in m.get("units", [])
                    ],
                }
            )

        return _ok({"count": len(results), "metrics": results})
    except Exception as e:
        return _err(e)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_biometrics(
    metric_id: int,
    unit_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get a biometric time series such as weight or body fat from Cronometer.

    Returns the recorded values over the date range as a list of
    {day, value} points.

    Use list_biometrics to find metric_id and unit_id (e.g. Weight is
    metric_id 1, with unit_id 1 for kg or 2 for lbs).

    Args:
        metric_id: Numeric metric ID from list_biometrics.
        unit_id: Numeric unit ID from the metric's units in list_biometrics.
        start_date: Start date as YYYY-MM-DD (defaults to 30 days ago).
        end_date: End date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        data = client.get_biometrics(
            metric_id,
            unit_id,
            start=_parse_date(start_date),
            end=_parse_date(end_date),
        )
        return _ok(
            {
                "metric_id": metric_id,
                "unit_id": unit_id,
                "start_date": start_date
                or str(date_module_today() - timedelta(days=30)),
                "end_date": end_date or str(date_module_today()),
                "biometrics": data,
            }
        )
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def date_module_today() -> date:
    """Return today's date in the account's timezone.

    Uses the authenticated client's timezone (resolved from the Cronometer
    account) so response echoes match the diary day an entry actually lands
    on, independent of the host clock. Extracted for easy mocking in tests.
    """
    return _get_client().today()


# ------------------------------------------------------------------
# Entrypoint (stdio only)
# ------------------------------------------------------------------


def main():
    """Run the MCP server over stdio.

    stdio is the only supported transport. For remote/hosted use the
    stdio process is wrapped by supergateway (see Dockerfile), which owns
    the HTTP listener; any HTTP exposure must sit behind an authenticating
    gateway/proxy.
    """
    # Load .env for local development (credentials). No-op if the file is
    # missing. override=False keeps real environment variables (Docker,
    # systemd, MCP client `env` blocks, etc.) authoritative over .env.
    from dotenv import find_dotenv, load_dotenv

    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path and load_dotenv(dotenv_path, override=False):
        logger.info("Loaded .env from %s", dotenv_path)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
