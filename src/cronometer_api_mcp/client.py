"""Cronometer mobile API client.

Reverse-engineered from the Cronometer Android/Flutter app (v4.52.6).
Communicates with mobile.cronometer.com/api/v2/* using clean JSON payloads.

Endpoint catalog was extracted via static analysis of libapp.so (Dart AOT
snapshot) from the APK. See the calorie-estimator project for the original
Frida-based traffic capture that established the auth flow and initial
endpoints.
"""

import logging
import os
from datetime import date, datetime

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://mobile.cronometer.com"

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
}


class CronometerError(Exception):
    """Raised when a Cronometer API call fails."""


class CronometerClient:
    """Stateful client for the Cronometer mobile API.

    Caches the auth token in memory and reuses it across requests.
    Re-authenticates automatically when the session expires.
    """

    def __init__(self) -> None:
        self._user_id: int | None = None
        self._token: str | None = None
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "user-agent": "Dart/3.9 (dart:io)",
                "content-type": "text/plain; charset=utf-8",
                "accept-encoding": "gzip",
            },
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

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
            "timezone": "America/New_York",
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
        logger.info(
            "Cronometer login successful (userId=%d, token=%s...)",
            self._user_id,
            self._token[:8] if self._token else "???",
        )

    def _ensure_auth(self) -> None:
        """Login lazily on first use."""
        if self._token is None:
            self.login()

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
        """Send a v2 POST request with JSON auth block. Re-authenticates once on failure."""
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
            self._token = None
            self.login()
            return self._request(endpoint, payload, _retried=True)

        resp.raise_for_status()
        data = resp.json()

        # Some endpoints return errors in the body
        if isinstance(data, dict) and data.get("result") == "FAILURE":
            if not _retried:
                logger.warning("Cronometer request failed, re-authenticating: %s", data)
                self._token = None
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

        url = f"/api/v3/user/{self._user_id}{path}"
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
            self._token = None
            self.login()
            return self._request_v3(method, path, json_body=json_body, _retried=True)

        return resp

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_day(d: date | None = None) -> str:
        """Format a date as Cronometer expects: non-zero-padded 'YYYY-M-D'."""
        d = d or date.today()
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
            measure_id: Measure/unit ID (from get_food). 0 for gram-based.
            grams: Weight in grams.
            translation_id: Translation ID (from search results, usually 0).
            day: Date to log to. Defaults to today.
            diary_group: Meal group. 0 = auto (based on time of day),
                         1 = Breakfast, 2 = Lunch, 3 = Dinner, 4 = Snacks.

        Returns the serving confirmation dict from the API.
        """
        now = datetime.now()
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
            "userId": self._user_id,
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

        to_day = to_day or date.today()
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

        end = end or date.today()
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
