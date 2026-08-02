"""Cronometer mobile API client.

Reverse-engineered from the Cronometer Android/Flutter app (v4.52.6).
Communicates with mobile.cronometer.com/api/v2/* using clean JSON payloads.

Endpoint catalog was extracted via static analysis of libapp.so (Dart AOT
snapshot) from the APK. See the calorie-estimator project for the original
Frida-based traffic capture that established the auth flow and initial
endpoints.
"""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://mobile.cronometer.com"

# Fallback timezone used only if the account's timezone can't be resolved from
# the login response. Matches the value historically assumed by this client.
_DEFAULT_TIMEZONE = "America/New_York"

# Optional deploy-time override for the account timezone. When set to a valid
# IANA zone name it is authoritative over both the login response and any
# cached value. This is the escape hatch for accounts whose server-side zone
# was clobbered by older builds (see issue #29) or when the resolved zone is
# otherwise wrong.
_ACCOUNT_TZ_ENV = "CRONOMETER_ACCOUNT_TZ"

# Cache the auth token across processes to avoid /api/v2/login rate limits.
# Cronometer throttles repeated logins per account; reusing a sessionKey lets
# short-lived CLI invocations behave like a long-running app.
_DEFAULT_SESSION_PATH = (
    Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "cronometer-mcp"
    / "session.json"
)

# Auth block sent with every request (mimics the Android app)
_APP_AUTH_TEMPLATE = {
    "api": 3,
    "os": "Android",
    "build": "2807",
    "flavour": "free",
}

# Cronometer nutrient IDs (from the login response nutrient list)
NUTRIENT_IDS = {
    "energy": 208,
    "protein": 203,
    "fat": 204,
    "carbs": 205,
    "fiber": 291,
    "sugar": 269,
    "sodium": 307,
    "alcohol": 221,
    "net_carbs": -1205,
    "saturated_fat": 606,
    "cholesterol": 601,
    "trans_fat": 605,
    "omega_3": 10001,
    "omega_6": 10002,
}

# Macro fields surfaced as a flat convenience block in the daily summary,
# mapped to their nutrient IDs. These are the values most relevant when
# summarizing a day at a glance.
SUMMARY_MACRO_IDS = {
    "energy": 208,
    "protein": 203,
    "carbs": 205,
    "net_carbs": -1205,
    "fat": 204,
    "fiber": 291,
    "alcohol": 221,
}


class CronometerError(Exception):
    """Raised when a Cronometer API call fails."""


class CronometerClient:
    """Stateful client for the Cronometer mobile API.

    Caches the auth token in memory and reuses it across requests.
    Re-authenticates automatically when the session expires.
    """

    def __init__(self, *, session_path: Path | None = None) -> None:
        self._user_id: int | None = None
        self._token: str | None = None
        # IANA timezone name of the Cronometer account, resolved from the login
        # response (or a restored session cache). Diary timestamps and "today"
        # are computed in this zone so behavior is independent of the host clock.
        self._timezone: str | None = None
        self._session_path: Path = session_path or _DEFAULT_SESSION_PATH
        # Cache of nutrient definitions (id -> {name, unit, category}).
        # Definitions are stable for an account, so fetch them once.
        self._nutrient_defs: dict[int, dict] | None = None
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "user-agent": "Dart/3.9 (dart:io)",
                "content-type": "text/plain; charset=utf-8",
                "accept-encoding": "gzip",
            },
            timeout=30.0,
        )
        self._load_cached_session()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _cache_key(self) -> str:
        """Tie the cached session to the configured username so
        switching accounts invalidates the cache automatically."""
        return os.getenv("CRONOMETER_USERNAME", "")

    def _load_cached_session(self) -> None:
        """Restore (user_id, token, timezone) from disk if a cache file exists.

        Silently ignores any read/parse error: the worst case is we
        re-login, which is the original behaviour. A cache written by an
        older version that predates timezone persistence is treated as
        invalid so the next login refreshes the account timezone.
        """
        try:
            raw = self._session_path.read_text()
        except FileNotFoundError, OSError:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("username") != self._cache_key():
            return
        token = data.get("token")
        user_id = data.get("user_id")
        timezone = data.get("timezone")
        # Reject caches that lack a stored timezone (pre-timezone schema) so we
        # re-login once and pick up the account zone rather than guessing.
        if not isinstance(timezone, str):
            return
        if isinstance(token, str) and isinstance(user_id, int):
            self._user_id = user_id
            self._token = token
            # A CRONOMETER_ACCOUNT_TZ override wins over the cached value so a
            # session.json poisoned by an older build (issue #29) can't defeat
            # an explicit deploy-time setting without invalidating the cache.
            self._timezone = self._resolve_timezone(timezone)
            logger.debug(
                "Restored Cronometer session for user_id=%d (tz=%s) from %s",
                user_id,
                self._timezone,
                self._session_path,
            )

    def _save_cached_session(self) -> None:
        """Persist (user_id, token, timezone) so future processes can reuse it."""
        if self._user_id is None or self._token is None:
            return
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._session_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "username": self._cache_key(),
                        "user_id": self._user_id,
                        "token": self._token,
                        "timezone": self._timezone,
                    }
                )
            )
            os.replace(tmp, self._session_path)
            try:
                os.chmod(self._session_path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.warning("Failed to persist Cronometer session: %s", exc)

    def _invalidate_session(self) -> None:
        """Drop the in-memory token and remove the cache file."""
        self._token = None
        self._timezone = None
        try:
            self._session_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove cached session: %s", exc)

    def _get_credentials(self) -> tuple[str, str]:
        username = os.getenv("CRONOMETER_USERNAME")
        password = os.getenv("CRONOMETER_PASSWORD")
        if not username or not password:
            raise CronometerError(
                "CRONOMETER_USERNAME and CRONOMETER_PASSWORD env vars must be set"
            )
        return username, password

    def login(self) -> None:
        """Authenticate with Cronometer and cache the session token."""
        username, password = self._get_credentials()

        payload = {
            "email": username,
            "password": password,
            # Must stay null: the login endpoint treats a non-null timezone as
            # a *write* that overwrites the account's server-side zone (verified
            # against the live API — sending "Asia/Tokyo" changed the account
            # setting and it persisted across subsequent logins). Older builds
            # hardcoded "America/New_York" here, silently resetting every user's
            # account zone to Eastern on each login (issue #29). Sending null
            # leaves the account setting untouched and the response echoes the
            # account's real zone.
            "timezone": None,
            "userCode": None,
            "build": "4.48.2 b2807-a",
            "device": "Android 14 (SDK 34), Google Pixel 6 Pro",
            "firebaseToken": "",
            "features": {
                "food_search_config": '{"newSearch": true, "newSpellcheck": true}',
                "use_gpt_autofill": "true",
            },
            "auth": {
                "userId": None,
                "token": None,
                **_APP_AUTH_TEMPLATE,
            },
            "lastSeen": 0,
            "config": {"call_version": 2},
        }

        logger.info("Logging in to Cronometer as %s", username)
        resp = self._http.post("/api/v2/login", json=payload)
        resp.raise_for_status()
        data = resp.json()

        if data.get("result") != "SUCCESS" and "sessionKey" not in data:
            raise CronometerError(f"Login failed: {data}")

        self._user_id = data["id"]
        self._token = data["sessionKey"]
        # The login response embeds the account profile, including the user's
        # configured IANA timezone. Prefer it over the host clock so diary
        # timestamps are correct regardless of where the server runs. A
        # CRONOMETER_ACCOUNT_TZ override, if set, wins over the response.
        self._timezone = self._resolve_timezone(data.get("timezone"))
        self._save_cached_session()
        logger.info(
            "Cronometer login successful (userId=%d, tz=%s, token=%s...)",
            self._user_id,
            self._timezone,
            self._token[:8] if self._token else "???",
        )

    def _ensure_auth(self) -> None:
        """Login lazily on first use."""
        if self._token is None:
            self.login()

    @property
    def user_id(self) -> int:
        """Authenticated user id; logs in first if needed (#30/#31)."""
        self._ensure_auth()
        assert self._user_id is not None
        return self._user_id

    def _auth_block(self) -> dict:
        return {
            "userId": self._user_id,
            "token": self._token,
            **_APP_AUTH_TEMPLATE,
        }

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _request(self, endpoint: str, payload: dict, *, _retried: bool = False) -> dict:
        """Send a v2 POST request with JSON auth block. Re-authenticates once on failure.

        Callers must build payloads from authenticated state (read identity via
        self.user_id, not self._user_id) so the retry can safely re-send the dict.
        """
        self._ensure_auth()

        payload["auth"] = self._auth_block()
        payload.setdefault("lastSeen", 0)

        logger.debug("Cronometer v2 request: POST %s", endpoint)
        resp = self._http.post(endpoint, json=payload)

        # Check for auth-related failures and retry once
        if resp.status_code in (401, 403) and not _retried:
            logger.warning(
                "Cronometer auth rejected (%d), re-authenticating",
                resp.status_code,
            )
            self._invalidate_session()
            self.login()
            return self._request(endpoint, payload, _retried=True)

        resp.raise_for_status()
        data = resp.json()

        # Some endpoints return errors in the body with an HTTP 200. An expired
        # session comes back as {"result": "FAIL", "error": "..."}; "FAILURE" is
        # kept defensively (never observed in real traffic, but harmless).
        if isinstance(data, dict) and data.get("result") in ("FAIL", "FAILURE"):
            if not _retried:
                logger.warning("Cronometer request failed, re-authenticating: %s", data)
                self._invalidate_session()
                self.login()
                return self._request(endpoint, payload, _retried=True)
            raise CronometerError(f"Cronometer API error: {data}")

        return data

    def _v3_headers(self) -> dict:
        """Headers for v3 REST API requests (auth via headers, not JSON body)."""
        return {
            "x-crono-session": self._token,
            "x-crono-app-os": "android",
            "x-crono-app-build-number": "2807",
            "x-crono-app-version": "4.48.2",
            "content-type": "application/json; charset=utf-8",
        }

    def _request_v3(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        _retried: bool = False,
    ) -> httpx.Response:
        """Send a v3 REST API request. Auth is via x-crono-session header.

        The v3 API uses RESTful conventions: HTTP verbs, path-based routing,
        and standard status codes (e.g. 204 for successful deletes).

        Returns the raw httpx.Response (caller handles status interpretation).
        """
        self._ensure_auth()

        url = f"/api/v3/user/{self.user_id}{path}"
        logger.debug("Cronometer v3 request: %s %s", method, url)

        resp = self._http.request(
            method, url, json=json_body, headers=self._v3_headers()
        )

        # Re-authenticate once on auth failures
        if resp.status_code in (401, 403) and not _retried:
            logger.warning(
                "Cronometer v3 auth rejected (%d), re-authenticating",
                resp.status_code,
            )
            self._invalidate_session()
            self.login()
            return self._request_v3(method, path, json_body=json_body, _retried=True)

        return resp

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _env_timezone() -> str | None:
        """Return a valid IANA zone from CRONOMETER_ACCOUNT_TZ, or None.

        An invalid name is logged and ignored so a typo can't hard-fail
        startup; resolution then falls through to the response/cache value.
        """
        name = os.getenv(_ACCOUNT_TZ_ENV)
        if not name:
            return None
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            logger.warning(
                "Ignoring invalid %s=%r (not a known IANA timezone)",
                _ACCOUNT_TZ_ENV,
                name,
            )
            return None
        return name

    def _resolve_timezone(self, response_tz: str | None) -> str:
        """Resolve the account timezone by priority.

        1. CRONOMETER_ACCOUNT_TZ env override (authoritative escape hatch).
        2. The value from the login response (trustworthy now that login()
           no longer overwrites the account's server-side zone; see #29).
        3. The historical default.
        """
        env = self._env_timezone()
        if env:
            return env
        if isinstance(response_tz, str) and response_tz:
            return response_tz
        return _DEFAULT_TIMEZONE

    def _tzinfo(self) -> ZoneInfo:
        """Return the account's timezone, falling back to the default.

        Resolved from the login response (or restored session cache). If the
        stored name is unset or unknown (e.g. a zone missing from the system
        tzdata), log once and fall back so stamping never hard-fails.
        """
        name = self._timezone or _DEFAULT_TIMEZONE
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            logger.warning(
                "Unknown account timezone %r; falling back to %s",
                name,
                _DEFAULT_TIMEZONE,
            )
            return ZoneInfo(_DEFAULT_TIMEZONE)

    def now(self) -> datetime:
        """Current wall-clock time in the account's timezone (aware)."""
        return datetime.now(self._tzinfo())

    def today(self) -> date:
        """Today's date in the account's timezone."""
        return self.now().date()

    def _format_day(self, d: date | None = None) -> str:
        """Format a date as Cronometer expects: non-zero-padded 'YYYY-M-D'.

        Defaults to today in the account's timezone, not the host clock.
        """
        d = d or self.today()
        return f"{d.year}-{d.month}-{d.day}"

    # ------------------------------------------------------------------
    # Food search
    # ------------------------------------------------------------------

    def search_food(self, query: str) -> list[dict]:
        """Search the Cronometer food database.

        Returns a list of food entries, each with keys:
        id, name, measureId, translationId, measureDisplayName, source,
        globalPopularity, score, etc.
        """
        payload = {
            "query": query,
            "tab": "ALL",
            "sources": ["All"],
            "config": {
                "newSearch": True,
                "newSpellcheck": True,
                "call_version": 1,
            },
        }
        data = self._request("/api/v2/find_food", payload)
        foods = data.get("foods", [])
        logger.info("Food search for %r returned %d results", query, len(foods))
        return foods

    # ------------------------------------------------------------------
    # Food details
    # ------------------------------------------------------------------

    def get_food(self, food_id: int) -> dict:
        """Fetch full food details, including server-assigned measure IDs.

        Returns the full food object with keys: id, name, measures,
        defaultMeasureId, nutrients, etc.
        """
        payload = {"id": food_id, "config": {"call_version": 1}}
        data = self._request("/api/v2/get_food", payload)
        logger.info(
            "Fetched food %d: %r (defaultMeasureId=%s)",
            food_id,
            data.get("name"),
            data.get("defaultMeasureId"),
        )
        return data

    def get_foods(self, food_ids: list[int]) -> list[dict]:
        """Batch-fetch full food details for many food IDs in one call.

        Mirrors get_food but resolves a list of IDs at once, which is how the
        Cronometer app resolves an entire day's diary. Returns a list of food
        objects, each with keys: id, name, source, measures, defaultMeasureId,
        nutrients, etc. Nutrient amounts are stored per-100g.

        Returns an empty list if food_ids is empty.
        """
        if not food_ids:
            return []
        payload = {"ids": list(food_ids), "config": {"call_version": 1}}
        data = self._request("/api/v2/get_foods", payload)
        foods = data.get("foods", []) if isinstance(data, dict) else []
        logger.info("Batch-fetched %d/%d foods", len(foods), len(food_ids))
        return foods

    # ------------------------------------------------------------------
    # Custom food creation
    # ------------------------------------------------------------------

    def create_custom_food(
        self,
        name: str,
        *,
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
    ) -> dict:
        """Create a custom food in Cronometer.

        Nutrient amounts are per the full serving (serving_grams).
        They are normalized to per-100g internally, since Cronometer stores
        all nutrient data on a per-100g basis.

        Returns {"food_id": int, "measure_id": int | None}.
        """
        # Cronometer stores nutrients per 100g -- normalize from per-serving.
        scale = 100.0 / serving_grams if serving_grams > 0 else 1.0

        net_carbs = max(0, carbs_g - fiber_g)

        nutrients = [
            {"id": NUTRIENT_IDS["energy"], "amount": round(calories * scale, 2)},
            {"id": NUTRIENT_IDS["protein"], "amount": round(protein_g * scale, 2)},
            {"id": NUTRIENT_IDS["fat"], "amount": round(fat_g * scale, 2)},
            {"id": NUTRIENT_IDS["carbs"], "amount": round(carbs_g * scale, 2)},
            {"id": NUTRIENT_IDS["fiber"], "amount": round(fiber_g * scale, 2)},
            {"id": NUTRIENT_IDS["sugar"], "amount": round(sugar_g * scale, 2)},
            {"id": NUTRIENT_IDS["sodium"], "amount": round(sodium_mg * scale, 2)},
            {
                "id": NUTRIENT_IDS["saturated_fat"],
                "amount": round(saturated_fat_g * scale, 2),
            },
            # Derived / calculated fields the app includes
            {"id": -203, "amount": round(protein_g * scale, 2)},
            {"id": -204, "amount": round(fat_g * scale, 2)},
            {"id": -205, "amount": round(carbs_g * scale, 2)},
            {"id": -221, "amount": 0},  # alcohol
            {"id": NUTRIENT_IDS["net_carbs"], "amount": round(net_carbs * scale, 2)},
        ]

        payload = {
            "data": {
                "id": 0,
                "name": name,
                "category": 0,
                "owner": None,
                "retired": None,
                "source": None,
                "defaultMeasureId": 0,
                "comments": None,
                "alternateId": None,
                "measures": [
                    {
                        "id": 0,
                        "name": serving_name,
                        "value": serving_grams,
                        "amount": 1.0,
                        "type": "Atomic",
                    }
                ],
                "labelType": "AMERICAN_2016",
                "nutrients": nutrients,
                "properties": {},
                "foodTags": [],
            },
            "config": {"call_version": 1},
        }

        data = self._request("/api/v2/add_food", payload)
        food_id = data.get("id")
        if not food_id:
            raise CronometerError(f"Failed to create custom food: {data}")

        logger.info("Created custom food %r (id=%d)", name, food_id)
        return {"food_id": food_id, "measure_id": None}

    # ------------------------------------------------------------------
    # Diary: add serving
    # ------------------------------------------------------------------

    def add_serving(
        self,
        food_id: int,
        measure_id: int | None,
        grams: float,
        translation_id: int = 0,
        day: date | None = None,
        diary_group: int = 0,
    ) -> dict:
        """Log a food serving to the diary.

        Args:
            food_id: Cronometer food ID.
            measure_id: Measure/unit ID. Get this from search_food() results
                        (measureId field) or get_food() (defaultMeasureId or measures[].id).
                        0 is only valid for user-created custom foods; database-sourced
                        foods (CRDB/NCCDB/FDC) require a real measure ID.
            grams: Weight in grams.
            translation_id: Translation ID (from search results, usually 0).
            day: Date to log to. Defaults to today.
            diary_group: Meal group. 0 = auto (based on time of day),
                         1 = Breakfast, 2 = Lunch, 3 = Dinner, 4 = Snacks.

        Returns the serving confirmation dict from the API.
        """
        now = self.now()
        day_str = self._format_day(day)
        time_str = f"{now.hour}:{now.minute}:{now.second}"

        if diary_group == 0:
            diary_group = _meal_group_for_hour(now.hour)

        serving = {
            "order": (diary_group << 16) | 1,
            "day": day_str,
            "time": time_str,
            "offset": None,
            "source": None,
            "userId": self.user_id,
            "servingId": None,
            "type": "Serving",
            "foodId": food_id,
            "measureId": measure_id or 0,
            "grams": grams,
            "translationId": translation_id,
        }

        payload = {
            "serving": serving,
            "config": {"call_version": 2},
        }

        data = self._request("/api/v2/add_serving", payload)
        logger.info(
            "Logged serving: food_id=%d, grams=%.1f, day=%s (serving_id=%s)",
            food_id,
            grams,
            day_str,
            data.get("id"),
        )
        return data

    # ------------------------------------------------------------------
    # Diary: get diary entries
    # ------------------------------------------------------------------

    def get_diary(self, day: date | None = None) -> dict:
        """Get all diary entries for a given day.

        Args:
            day: Date to fetch. Defaults to today.

        Returns the full diary response from the API.
        """
        payload = {
            "day": self._format_day(day),
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/get_diary", payload)
        logger.info("Fetched diary for %s", self._format_day(day))
        return data

    # ------------------------------------------------------------------
    # Diary: delete entries
    # ------------------------------------------------------------------

    def delete_entries(self, entry_ids: list[str], day: date | None = None) -> dict:
        """Remove diary entries by their serving IDs.

        Fetches the diary for the given day, matches entries by servingId,
        and sends the full serving objects to the v3 DELETE endpoint.

        Uses: DELETE /api/v3/user/{userId}/diary-entries

        Args:
            entry_ids: List of serving IDs to delete (as strings).
            day: The day the entries belong to. Defaults to today.

        Returns dict with removed IDs and count.
        """
        # Fetch the diary to get full serving objects (required by v3 API)
        diary_data = self.get_diary(day)
        diary_entries = diary_data.get("diary", [])

        id_set = set(str(eid) for eid in entry_ids)
        to_delete = []
        for entry in diary_entries:
            if str(entry.get("servingId")) in id_set:
                to_delete.append(entry)

        if not to_delete:
            logger.warning(
                "None of the requested entry IDs found in diary for %s",
                self._format_day(day),
            )
            return {"removed": [], "count": 0}

        resp = self._request_v3(
            "DELETE",
            "/diary-entries",
            json_body={"diaryEntries": to_delete},
        )

        if resp.status_code == 204:
            removed_ids = [str(e["servingId"]) for e in to_delete]
            logger.info(
                "Deleted %d entries for %s: %s",
                len(removed_ids),
                self._format_day(day),
                removed_ids,
            )
            return {"removed": removed_ids, "count": len(removed_ids)}
        else:
            raise CronometerError(
                f"Delete failed with status {resp.status_code}: {resp.text[:300]}"
            )

    # ------------------------------------------------------------------
    # Diary: mark day complete
    # ------------------------------------------------------------------

    def mark_day_complete(self, day: date | None = None, complete: bool = True) -> dict:
        """Mark a diary day as complete or incomplete.

        Args:
            day: Date to mark. Defaults to today.
            complete: True to mark complete, False for incomplete.

        Returns the API response.
        """
        payload = {
            "day": self._format_day(day),
            "complete": complete,
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/set_complete", payload)
        status = "complete" if complete else "incomplete"
        logger.info("Marked %s as %s", self._format_day(day), status)
        return data

    # ------------------------------------------------------------------
    # Diary: copy from yesterday
    # ------------------------------------------------------------------

    def copy_day(
        self, from_day: date | None = None, to_day: date | None = None
    ) -> dict:
        """Copy all diary entries from one day to another.

        Uses: POST /api/v2/copy

        Args:
            from_day: Source date. Defaults to yesterday.
            to_day: Destination date. Defaults to today.

        Returns the API response with the copied entries.
        """
        from datetime import timedelta

        to_day = to_day or self.today()
        from_day = from_day or (to_day - timedelta(days=1))

        payload = {
            "from": self._format_day(from_day),
            "to": self._format_day(to_day),
            "diaryGroupNumber": None,
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/copy", payload)
        logger.info(
            "Copied entries from %s to %s",
            self._format_day(from_day),
            self._format_day(to_day),
        )
        return data

    # ------------------------------------------------------------------
    # Nutrition: get nutrients
    # ------------------------------------------------------------------

    def get_nutrients(self, day: date | None = None) -> dict:
        """Get nutrient totals for a given day.

        Args:
            day: Date to fetch. Defaults to today.

        Returns the nutrient summary from the API.
        """
        payload = {
            "day": self._format_day(day),
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/get_nutrients", payload)
        logger.info("Fetched nutrients for %s", self._format_day(day))
        return data

    def get_nutrition_scores(
        self, day: date | None = None, *, include_supplements: bool = True
    ) -> dict:
        """Get nutrition scores with per-nutrient consumed amounts.

        This is the richest nutrition endpoint -- it returns category scores
        (All Targets, Vitamins, Minerals, Electrolytes, Antioxidants, Immune
        Support, Metabolism, Bone Health, etc.) with the actual consumed amount
        and confidence level for each nutrient.

        Automatically fetches the diary to obtain serving IDs.

        Uses: POST /api/v2/get_nutrition_scores

        Args:
            day: Date to score. Defaults to today.
            include_supplements: Whether to include supplements in scoring.

        Returns the nutrition scores from the API.
        """
        diary_data = self.get_diary(day)
        diary_entries = diary_data.get("diary", [])

        serving_ids = [
            e["servingId"]
            for e in diary_entries
            if e.get("type") == "Serving" and "servingId" in e
        ]

        payload = {
            "startDay": "1900-1-1",
            "endDay": "1900-1-1",
            "servingIds": serving_ids,
            "supplements": "true" if include_supplements else "false",
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/get_nutrition_scores", payload)
        logger.info(
            "Fetched nutrition scores for %s (%d servings)",
            self._format_day(day),
            len(serving_ids),
        )
        return data

    def get_nutrient_definitions(self) -> dict[int, dict]:
        """Get the nutrient definition map (id -> {name, unit, category}).

        The get_nutrients endpoint returns the account's nutrient catalog --
        names, units, RDIs, and categories -- not consumed amounts. We use it
        purely to label nutrient IDs. Cached after the first call since the
        catalog is stable.
        """
        if self._nutrient_defs is None:
            data = self.get_nutrients()
            defs: dict[int, dict] = {}
            for n in data.get("nutrients", []):
                nid = n.get("id")
                if nid is None:
                    continue
                defs[nid] = {
                    "name": n.get("name"),
                    "unit": n.get("unit"),
                    "category": n.get("category"),
                }
            self._nutrient_defs = defs
        return self._nutrient_defs

    def get_consumed_nutrients(self, day: date | None = None) -> dict:
        """Get consumed nutrient totals for a day, labeled and summarized.

        Builds a clean summary from the server-computed per-nutrient totals in
        get_nutrition_scores (the "All Targets" category), which reflect exactly
        the nutrients the user is tracking (i.e. has targets set for). Each
        nutrient is labeled with its name, unit, and category via the nutrient
        definition catalog.

        Returns a dict:
            {
                "macros": {energy, protein, carbs, net_carbs, fat, fiber,
                           alcohol},  # flat amounts (None if not tracked)
                "nutrients": [
                    {id, name, amount, unit, category, confidence}, ...
                ],
            }

        Note: a nutrient only appears if the user tracks it in Cronometer. To
        see e.g. saturated fat, the user must have a target set for it.
        """
        scores = self.get_nutrition_scores(day)

        # The "All Targets" category contains every tracked nutrient.
        all_targets = next(
            (c for c in scores.get("scores", []) if c.get("title") == "All Targets"),
            None,
        )
        components = (all_targets or {}).get("components", []) if all_targets else []

        defs = self.get_nutrient_definitions()

        nutrients: list[dict] = []
        amounts_by_id: dict[int, float] = {}
        for comp in components:
            nid = comp.get("nutrientId")
            if nid is None:
                continue
            amount = comp.get("amount")
            amounts_by_id[nid] = amount
            meta = defs.get(nid, {})
            nutrients.append(
                {
                    "id": nid,
                    "name": meta.get("name"),
                    "amount": amount,
                    "unit": meta.get("unit"),
                    "category": meta.get("category"),
                    "confidence": comp.get("confidence"),
                }
            )

        macros = {key: amounts_by_id.get(nid) for key, nid in SUMMARY_MACRO_IDS.items()}

        logger.info(
            "Built consumed nutrient summary for %s (%d tracked nutrients)",
            self._format_day(day),
            len(nutrients),
        )
        return {"macros": macros, "nutrients": nutrients}

    def enrich_diary_servings(self, diary: dict) -> dict:
        """Merge food metadata into a raw get_diary payload (best-effort).

        Diary "Serving" entries carry only numeric IDs (foodId, measureId,
        grams). This resolves each foodId via a single batch get_foods call and
        merges per-entry:

          - name, source, category: from the food object
          - measure: {measure_id, name, grams_per_unit} for the entry's
            measureId (falls back to the food's defaultMeasureId)
          - servings: grams / grams_per_unit, when derivable
          - nutrients: the food's nutrient profile scaled to the entry's amount
            (per-100g for Weight/Atomic measures, per-serving for Recipe
            measures), labeled with name/unit/category via the nutrient
            definitions catalog

        Enrichment is best-effort: if the get_foods call fails or a food is not
        returned, the corresponding entries are left unchanged. The diary dict
        is mutated in place and also returned. Non-Serving entries (Exercise,
        Biometric) already carry a name and are left untouched.
        """
        if not isinstance(diary, dict):
            return diary
        entries = diary.get("diary")
        if not isinstance(entries, list):
            return diary

        food_ids = sorted(
            {
                e["foodId"]
                for e in entries
                if isinstance(e, dict)
                and e.get("type") == "Serving"
                and isinstance(e.get("foodId"), int)
            }
        )
        if not food_ids:
            return diary

        try:
            foods = self.get_foods(food_ids)
        except Exception as exc:  # best-effort: keep diary without names
            logger.warning("Diary enrichment skipped (get_foods failed): %s", exc)
            return diary

        food_by_id = {f.get("id"): f for f in foods if isinstance(f, dict)}
        try:
            defs = self.get_nutrient_definitions()
        except Exception:
            defs = {}

        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "Serving":
                continue
            food = food_by_id.get(entry.get("foodId"))
            if not food:
                continue

            entry["name"] = food.get("name")
            entry["source"] = food.get("source")
            if food.get("category") is not None:
                entry["category"] = food.get("category")

            measures = {
                m.get("id"): m for m in food.get("measures", []) if isinstance(m, dict)
            }
            measure = measures.get(entry.get("measureId")) or measures.get(
                food.get("defaultMeasureId")
            )
            grams = entry.get("grams")
            if measure:
                grams_per_unit = measure.get("value")
                entry["measure"] = {
                    "measure_id": measure.get("id"),
                    "name": measure.get("name"),
                    "grams_per_unit": grams_per_unit,
                }
                if (
                    isinstance(grams, (int, float))
                    and isinstance(grams_per_unit, (int, float))
                    and grams_per_unit
                ):
                    entry["servings"] = round(grams / grams_per_unit, 4)

            # Nutrient scaling depends on the measure type:
            #   - Recipe measures: nutrients are stored per one reference
            #     serving and the diary "grams" field is a serving count, so
            #     scale by grams directly.
            #   - Weight/Atomic measures: nutrients are stored per-100g and
            #     "grams" is real grams, so scale by grams / 100.
            if isinstance(grams, (int, float)):
                if measure and measure.get("type") == "Recipe":
                    scale = grams
                else:
                    scale = grams / 100.0
                scaled: list[dict] = []
                for n in food.get("nutrients", []):
                    if not isinstance(n, dict):
                        continue
                    nid = n.get("id")
                    amount = n.get("amount")
                    if nid is None or not isinstance(amount, (int, float)):
                        continue
                    meta = defs.get(nid, {})
                    scaled.append(
                        {
                            "id": nid,
                            "name": meta.get("name"),
                            "amount": round(amount * scale, 4),
                            "unit": meta.get("unit"),
                            "category": meta.get("category"),
                        }
                    )
                entry["nutrients"] = scaled

        logger.info("Enriched %d diary foods with names/nutrients", len(food_by_id))
        return diary

    # ------------------------------------------------------------------
    # Macro targets
    # ------------------------------------------------------------------

    def get_macro_schedules(self) -> dict:
        """Get the weekly macro target schedule.

        Returns the schedule mapping days of week to macro templates.
        """
        payload = {"config": {"call_version": 1}}
        data = self._request("/api/v2/get_macro_schedules", payload)
        logger.info("Fetched macro schedules")
        return data

    def get_macro_target_templates(self) -> dict:
        """Get all saved macro target templates.

        Returns the list of macro target templates with their values.
        """
        payload = {"config": {"call_version": 1}}
        data = self._request("/api/v2/get_macro_target_templates", payload)
        logger.info("Fetched macro target templates")
        return data

    # ------------------------------------------------------------------
    # Fasting
    # ------------------------------------------------------------------

    def get_fasting_with_date_range(
        self, start: date | None = None, end: date | None = None
    ) -> dict:
        """Get fasting history for a date range.

        Args:
            start: Start date. Defaults to 30 days ago.
            end: End date. Defaults to today.

        Returns fasting entries from the API.
        """
        from datetime import timedelta

        end = end or self.today()
        start = start or (end - timedelta(days=30))

        payload = {
            "start": self._format_day(start),
            "end": self._format_day(end),
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/get_fasting_with_date_range", payload)
        logger.info("Fetched fasting data %s to %s", start, end)
        return data

    def get_fasting_stats(self) -> dict:
        """Get aggregate fasting statistics.

        Returns total fasting hours, longest fast, averages, etc.
        """
        payload = {"config": {"call_version": 1}}
        data = self._request("/api/v2/get_fasting_stats", payload)
        logger.info("Fetched fasting stats")
        return data

    # ------------------------------------------------------------------
    # Biometrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> list[dict]:
        """Get the biometric metric catalog.

        Each metric describes one trackable biometric (Weight, Body Fat,
        Heart Rate, Blood Glucose, Waist Size, ...) with keys: id, name,
        legacy (bool), and units -- a list of {id, name, ...} the value can
        be expressed in. Cronometer stores every biometric under one of
        these metric IDs.

        Uses: POST /api/v2/get_metrics
        """
        payload = {"config": {"call_version": 1}}
        data = self._request("/api/v2/get_metrics", payload)
        return data.get("metrics", [])

    def get_biometrics(
        self,
        metric_id: int,
        unit_id: int,
        start: date | None = None,
        end: date | None = None,
    ) -> dict:
        """Get a biometric time series (e.g. weight, body fat) over a range.

        Args:
            metric_id: Metric ID from get_metrics (e.g. 1 for Weight).
            unit_id: Unit ID from the metric's units list (e.g. 1 for kg).
            start: Start date. Defaults to 30 days before `end`.
            end: End date. Defaults to today.

        Uses: POST /api/v2/get_biometrics

        Returns the API response: {"data": [{"day": "YYYY-MM-DD", "value": float}, ...]}.
        """
        from datetime import timedelta

        end = end or self.today()
        start = start or (end - timedelta(days=30))

        payload = {
            "metricId": metric_id,
            "unitId": unit_id,
            "start": self._format_day(start),
            "end": self._format_day(end),
            "config": {"call_version": 1},
        }
        data = self._request("/api/v2/get_biometrics", payload)
        logger.info(
            "Fetched biometrics for metric %d (unit %d) %s to %s",
            metric_id,
            unit_id,
            self._format_day(start),
            self._format_day(end),
        )
        return data


# ======================================================================
# Helpers
# ======================================================================


def _meal_group_for_hour(hour: int) -> int:
    """Map hour of day to a Cronometer diary meal group.

    1 = Breakfast, 2 = Lunch, 3 = Dinner, 4 = Snacks.
    """
    if 4 <= hour < 10:
        return 1  # Breakfast
    elif 10 <= hour < 14:
        return 2  # Lunch
    elif 14 <= hour < 21:
        return 3  # Dinner
    else:
        return 4  # Snacks
