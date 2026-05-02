"""MCP server for Cronometer nutrition data via the mobile REST API."""

import json
import logging
import os
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

    Returns every food entry logged for the day, including food names,
    amounts, meal groups, and nutrient data.

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

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_diary(day)

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

        return _ok(
            {
                "date": date or str(date_module_today()),
                "energy_summary": energy_summary,
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
    """Get daily nutrition summary with macro and micronutrient totals.

    Returns calorie, protein, carb, fat, fiber totals and micronutrients
    for the given day.

    Args:
        date: Date as YYYY-MM-DD (defaults to today).
    """
    try:
        client = _get_client()
        day = _parse_date(date)
        data = client.get_nutrients(day)
        return _ok(
            {
                "date": date or str(date_module_today()),
                "nutrients": data,
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
                "start_date": start_date or str(date.today() - timedelta(days=30)),
                "end_date": end_date or str(date.today()),
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
# Helpers
# ------------------------------------------------------------------


def date_module_today() -> date:
    """Return today's date (extracted for easy mocking in tests)."""
    return date.today()


# ------------------------------------------------------------------
# OAuth 2.1 Authorization Code + PKCE for remote transports
# ------------------------------------------------------------------


class OAuthAuthorizationMiddleware:
    """ASGI middleware implementing OAuth 2.1 authorization code flow with PKCE.

    This is a minimal single-user OAuth server that satisfies Claude.ai's
    MCP connector requirements. It implements:

    - GET /.well-known/oauth-authorization-server → server metadata (RFC 8414)
    - GET /.well-known/oauth-protected-resource → resource metadata
    - GET /authorize → authorization page (user clicks to approve)
    - POST /token → authorization code → access token exchange (with PKCE)
    - Bearer token validation on all other HTTP requests

    Auth codes are single-use and short-lived (in-memory, lost on restart).
    Since this is a single-user server, the "authorization" is just confirming
    you're the server owner.

    Env vars:
    - MCP_OAUTH_CLIENT_ID: Expected client_id from Claude.ai
    - MCP_OAUTH_CLIENT_SECRET: Expected client_secret from Claude.ai
    - MCP_AUTH_TOKEN: The access token issued and validated
    - MCP_BASE_URL: Public base URL (for metadata endpoints)
    """

    def __init__(
        self,
        app,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        base_url: str = "",
    ):
        self.app = app
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        # In-memory store for pending auth codes: code -> {code_challenge, redirect_uri, state}
        self._pending_codes: dict[str, dict] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # OAuth metadata discovery (RFC 8414)
        if path == "/.well-known/oauth-authorization-server" and method == "GET":
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {
                    "issuer": self.base_url,
                    "authorization_endpoint": f"{self.base_url}/authorize",
                    "token_endpoint": f"{self.base_url}/token",
                    "registration_endpoint": f"{self.base_url}/register",
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_post",
                    ],
                    "grant_types_supported": [
                        "authorization_code",
                    ],
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["mcp"],
                }
            )
            await response(scope, receive, send)
            return

        # OAuth Protected Resource metadata
        if path == "/.well-known/oauth-protected-resource" and method == "GET":
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {
                    "resource": self.base_url,
                    "authorization_servers": [self.base_url],
                    "scopes_supported": ["mcp"],
                }
            )
            await response(scope, receive, send)
            return

        # Dynamic client registration (stub -- just echoes back the client_id)
        if path == "/register" and method == "POST":
            await self._handle_register(scope, receive, send)
            return

        # Authorization endpoint
        if path == "/authorize" and method == "GET":
            await self._handle_authorize(scope, receive, send)
            return

        # Authorize form submission
        if path == "/authorize" and method == "POST":
            await self._handle_authorize_submit(scope, receive, send)
            return

        # Token endpoint
        if path == "/token" and method == "POST":
            await self._handle_token(scope, receive, send)
            return

        # Favicon (don't 401 on this during authorize page load)
        if path == "/favicon.ico":
            from starlette.responses import Response

            await Response(status_code=404)(scope, receive, send)
            return

        # All other requests: validate bearer token
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()
        if auth_value != f"Bearer {self.access_token}":
            from starlette.responses import Response

            # Return 401 with WWW-Authenticate header per MCP spec
            response = Response(
                content='{"error": "unauthorized"}',
                status_code=401,
                headers={
                    "WWW-Authenticate": "Bearer",
                    "Content-Type": "application/json",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _handle_register(self, scope, receive, send):
        """Handle POST /register (OAuth 2.0 Dynamic Client Registration).

        Minimal implementation that accepts any registration and returns
        the provided client_name with pre-configured credentials.
        """
        from starlette.responses import JSONResponse

        body = await self._read_body(receive)
        try:
            import json as _json

            data = _json.loads(body)
        except Exception:
            data = {}

        response = JSONResponse(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "client_name": data.get("client_name", "mcp-client"),
                "redirect_uris": data.get("redirect_uris", []),
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
            status_code=201,
        )
        await response(scope, receive, send)

    async def _handle_authorize(self, scope, receive, send):
        """Handle GET /authorize -- show a simple approval page."""
        from starlette.responses import HTMLResponse
        from urllib.parse import parse_qs

        query = scope.get("query_string", b"").decode()
        params = parse_qs(query)

        client_id = params.get("client_id", [None])[0]
        redirect_uri = params.get("redirect_uri", [None])[0]
        code_challenge = params.get("code_challenge", [None])[0]
        code_challenge_method = params.get("code_challenge_method", [None])[0]
        state = params.get("state", [None])[0]
        response_type = params.get("response_type", [None])[0]

        if response_type != "code" or not redirect_uri or not code_challenge:
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "invalid_request"}, status_code=400)
            await response(scope, receive, send)
            return

        # Render a simple approval page
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Authorize MCP Access</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 60px auto; padding: 20px; }}
        h1 {{ font-size: 1.4em; }}
        .info {{ background: #f0f4f8; padding: 16px; border-radius: 8px; margin: 20px 0; }}
        button {{ background: #2563eb; color: white; border: none; padding: 12px 32px;
                 border-radius: 6px; font-size: 16px; cursor: pointer; }}
        button:hover {{ background: #1d4ed8; }}
    </style>
</head>
<body>
    <h1>Authorize Cronometer MCP</h1>
    <div class="info">
        <p><strong>Client:</strong> {client_id or "unknown"}</p>
        <p>This will grant access to your Cronometer nutrition data
           through the MCP protocol.</p>
    </div>
    <form method="POST" action="/authorize">
        <input type="hidden" name="client_id" value="{client_id or ""}">
        <input type="hidden" name="redirect_uri" value="{redirect_uri or ""}">
        <input type="hidden" name="code_challenge" value="{code_challenge or ""}">
        <input type="hidden" name="code_challenge_method" value="{code_challenge_method or ""}">
        <input type="hidden" name="state" value="{state or ""}">
        <button type="submit">Authorize</button>
    </form>
</body>
</html>"""
        response = HTMLResponse(html)
        await response(scope, receive, send)

    async def _handle_authorize_submit(self, scope, receive, send):
        """Handle POST /authorize -- user approved, generate code and redirect."""
        from starlette.responses import RedirectResponse
        from urllib.parse import parse_qs, urlencode
        import secrets

        body = await self._read_body(receive)
        params = parse_qs(body.decode("utf-8"))

        redirect_uri = params.get("redirect_uri", [None])[0]
        code_challenge = params.get("code_challenge", [None])[0]
        code_challenge_method = params.get("code_challenge_method", [None])[0]
        state = params.get("state", [None])[0]

        if not redirect_uri or not code_challenge:
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "invalid_request"}, status_code=400)
            await response(scope, receive, send)
            return

        # Generate a one-time auth code
        code = secrets.token_urlsafe(32)
        self._pending_codes[code] = {
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method or "S256",
            "redirect_uri": redirect_uri,
        }

        # Redirect back to Claude.ai with the code
        callback_params = {"code": code}
        if state:
            callback_params["state"] = state
        separator = "&" if "?" in redirect_uri else "?"
        redirect_url = f"{redirect_uri}{separator}{urlencode(callback_params)}"

        response = RedirectResponse(url=redirect_url, status_code=302)
        await response(scope, receive, send)

    async def _handle_token(self, scope, receive, send):
        """Handle POST /token (authorization code exchange with PKCE)."""
        from starlette.responses import JSONResponse
        from urllib.parse import parse_qs
        import hashlib
        import base64

        body = await self._read_body(receive)
        try:
            params = parse_qs(body.decode("utf-8"))
        except Exception:
            response = JSONResponse({"error": "invalid_request"}, status_code=400)
            await response(scope, receive, send)
            return

        grant_type = params.get("grant_type", [None])[0]
        code = params.get("code", [None])[0]
        code_verifier = params.get("code_verifier", [None])[0]

        if grant_type != "authorization_code":
            response = JSONResponse(
                {"error": "unsupported_grant_type"}, status_code=400
            )
            await response(scope, receive, send)
            return

        if not code or not code_verifier:
            response = JSONResponse({"error": "invalid_request"}, status_code=400)
            await response(scope, receive, send)
            return

        # Look up and consume the auth code (single-use)
        pending = self._pending_codes.pop(code, None)
        if pending is None:
            response = JSONResponse({"error": "invalid_grant"}, status_code=400)
            await response(scope, receive, send)
            return

        # Validate PKCE: S256(code_verifier) must match code_challenge
        code_challenge = pending["code_challenge"]
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed_challenge = (
            base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        )

        if computed_challenge != code_challenge:
            response = JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "PKCE verification failed",
                },
                status_code=400,
            )
            await response(scope, receive, send)
            return

        # Issue access token
        response = JSONResponse(
            {
                "access_token": self.access_token,
                "token_type": "bearer",
                "scope": "mcp",
            }
        )
        await response(scope, receive, send)

    @staticmethod
    async def _read_body(receive) -> bytes:
        """Read the full request body from ASGI receive."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        return body


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main():
    # Load .env for local development. No-op if the file is missing.
    # override=False keeps real environment variables (Docker, systemd,
    # MCP client `env` blocks, etc.) authoritative over .env.
    from dotenv import find_dotenv, load_dotenv

    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path and load_dotenv(dotenv_path, override=False):
        logger.info("Loaded .env from %s", dotenv_path)

    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport == "stdio":
        mcp.run(transport="stdio")

    elif transport in ("sse", "streamable-http"):
        import uvicorn

        host = os.getenv("FASTMCP_HOST", "0.0.0.0")
        port = int(os.getenv("PORT", os.getenv("FASTMCP_PORT", "8000")))

        if transport == "sse":
            app = mcp.sse_app()
        else:
            app = mcp.streamable_http_app()

        # OAuth config for remote transports
        client_id = os.getenv("MCP_OAUTH_CLIENT_ID", "")
        client_secret = os.getenv("MCP_OAUTH_CLIENT_SECRET", "")
        access_token = os.getenv("MCP_AUTH_TOKEN")
        base_url = os.getenv("MCP_BASE_URL", f"http://localhost:{port}")

        if access_token:
            logger.info("OAuth authorization code flow enabled")
            app = OAuthAuthorizationMiddleware(
                app,
                client_id=client_id,
                client_secret=client_secret,
                access_token=access_token,
                base_url=base_url,
            )
        else:
            logger.warning("No auth configured -- server is unauthenticated")

        logger.info("Starting %s transport on %s:%d", transport, host, port)
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        server.run()

    else:
        raise ValueError(
            f"Unknown MCP_TRANSPORT: {transport!r}. "
            "Must be 'stdio', 'sse', or 'streamable-http'."
        )


if __name__ == "__main__":
    main()
