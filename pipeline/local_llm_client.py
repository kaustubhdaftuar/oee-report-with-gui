from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

try:
    from prompts import (
        REPORT_SYSTEM_PROMPT,
        build_prompt,
    )
except ImportError:
    from .prompts import (
        REPORT_SYSTEM_PROMPT,
        build_prompt,
    )


# ============================================================
# CONFIGURATION
# ============================================================

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"

MODEL_NAME = "gemma-2-2b-it"

MAX_NEW_TOKENS = 700

REQUEST_TIMEOUT = 180

TEMPERATURE = 0.1

MAX_DOWNTIME_POINTS = 10

MAX_QUALITY_POINTS = 10

MAX_SPOUT_POINTS = 10


# ============================================================
# GENERAL HELPERS
# ============================================================

def fail(message: str) -> None:
    raise RuntimeError(message)


def emit_message(
    message: str,
    status_callback=None,
    progress_callback=None,
) -> None:

    print(message)

    if status_callback:
        try:
            status_callback(message)
        except Exception:
            pass

    if progress_callback:
        try:
            progress_callback(message)
        except Exception:
            pass


def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:

    if value is None:
        return default

    if isinstance(value, bool):
        return float(value)

    if isinstance(value, (int, float)):
        return float(value)

    try:

        text = str(value).strip()

        if not text:
            return default

        text = (
            text
            .replace("%", "")
            .replace(",", "")
            .strip()
        )

        return float(text)

    except Exception:

        return default


def _safe_int(
    value: Any,
    default: int = 0,
) -> int:

    try:

        return int(
            round(
                _safe_float(
                    value,
                    default,
                )
            )
        )

    except Exception:

        return default


def _raw(
    value: Any,
    default: Any = None,
) -> Any:
    """
    Extract a raw value from common analysis.json structures.

    Supported:

        123
        "123"
        {"raw_value": 123}
        {"raw": 123}
        {"value": 123}
    """

    if value is None:
        return default

    if isinstance(value, dict):

        raw_value = value.get("raw_value")

        if raw_value is None:
            raw_value = value.get("raw")

        if raw_value is None:
            raw_value = value.get("value")

        if raw_value is None:
            raw_value = value.get("percentage")

        if raw_value is None:
            return default

        return raw_value

    return value


def _find_nested_value(
    obj: Any,
    possible_keys: list[str],
) -> Any:
    """
    Recursively find the first matching key.

    Used as a compatibility layer because analysis.json
    has evolved between pipeline versions.
    """

    if isinstance(obj, dict):

        # First check the current dictionary itself.
        for key in possible_keys:

            if key in obj:

                value = obj[key]

                if value is not None:
                    return value

        # Then recurse.
        for value in obj.values():

            result = _find_nested_value(
                value,
                possible_keys,
            )

            if result is not None:
                return result

    elif isinstance(obj, list):

        for item in obj:

            result = _find_nested_value(
                item,
                possible_keys,
            )

            if result is not None:
                return result

    return None


def _metric_value(
    data: Dict[str, Any],
    section: str,
    key: str,
    default: float = 0.0,
) -> float:
    """
    Read a metric from a specific analysis.json section.
    """

    section_data = data.get(section, {})

    if not isinstance(
        section_data,
        dict,
    ):
        return default

    value = section_data.get(key)

    if value is None:
        return default

    return _safe_float(
        _raw(
            value,
            default,
        ),
        default,
    )


# ============================================================
# LM STUDIO
# ============================================================

def check_lm_studio() -> str:
    """
    Verify that LM Studio is reachable and that a usable model
    is available.

    Returns the exact model identifier that should be sent to
    the OpenAI-compatible endpoint. This avoids failures when
    LM Studio exposes a model ID that differs slightly from the
    local MODEL_NAME setting.
    """

    base_url = LM_STUDIO_URL.rsplit(
        "/v1/",
        1,
    )[0]

    try:
        response = requests.get(
            f"{base_url}/v1/models",
            timeout=10,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            "Unable to connect to LM Studio.\n\n"
            f"Expected server:\n{LM_STUDIO_URL}\n\n"
            "Make sure:\n"
            "1. LM Studio is running.\n"
            "2. The Gemma model is loaded.\n"
            "3. The LM Studio local server is started."
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError(
            "LM Studio local server returned "
            f"HTTP {response.status_code}.\n\n"
            f"Response:\n{response.text}"
        )

    try:
        model_data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "LM Studio /v1/models returned invalid JSON.\n\n"
            f"Response:\n{response.text}"
        ) from exc

    model_entries = model_data.get("data", [])
    model_ids = []

    if isinstance(model_entries, list):
        for entry in model_entries:
            if isinstance(entry, dict):
                model_id = entry.get("id")
                if model_id:
                    model_ids.append(str(model_id))

    if not model_ids:
        raise RuntimeError(
            "LM Studio is reachable, but /v1/models returned no "
            "loaded models.\n\n"
            "Load the Gemma model in LM Studio and make sure the "
            "local server is running."
        )

    # Prefer the configured model if LM Studio exposes it.
    if MODEL_NAME in model_ids:
        return MODEL_NAME

    # Small/local LM Studio setups commonly have exactly one loaded model.
    # In that case, use its exact server-side ID automatically.
    if len(model_ids) == 1:
        selected = model_ids[0]
        emit_message(
            f"[LLM] Configured model '{MODEL_NAME}' was not found. "
            f"Using the only loaded LM Studio model: '{selected}'."
        )
        return selected

    raise RuntimeError(
        "Configured LM Studio model was not found.\n\n"
        f"Configured model: {MODEL_NAME}\n"
        "Models exposed by LM Studio:\n"
        + "\n".join(f"  - {model_id}" for model_id in model_ids)
        + "\n\n"
        "Either load the configured Gemma model or update MODEL_NAME "
        "in local_llm_client.py to one of the IDs above."
    )

def generate_text(
    prompt: str,
    system_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
) -> str:
    """
    Send the verified report data to Gemma through LM Studio.

    The system instructions and user data are kept separate.
    HTTP error bodies are included in failures so LM Studio's
    actual reason for a 400 response is visible.
    """

    if system_prompt is None:
        system_prompt = REPORT_SYSTEM_PROMPT

    if model_name is None:
        model_name = check_lm_studio()

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_NEW_TOKENS,
        "stream": False,
    }

    last_error = None

    for attempt in range(2):
        try:
            response = requests.post(
                LM_STUDIO_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code >= 400:
                # Do not hide LM Studio's response body. It usually
                # contains the exact reason for HTTP 400.
                raise RuntimeError(
                    "LM Studio returned HTTP "
                    f"{response.status_code}.\n\n"
                    f"Response body:\n{response.text}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "LM Studio returned a non-JSON response.\n\n"
                    f"Response body:\n{response.text}"
                ) from exc

            choices = data.get(
                "choices",
                [],
            )

            if not choices:
                raise RuntimeError(
                    "LM Studio returned no choices.\n\n"
                    f"Response body:\n{response.text}"
                )

            message = choices[0].get(
                "message",
                {},
            )

            content = message.get(
                "content",
                "",
            )

            if not isinstance(
                content,
                str,
            ):
                content = str(content)

            content = content.strip()

            if not content:
                raise RuntimeError(
                    "LM Studio returned an empty response.\n\n"
                    f"Response body:\n{response.text}"
                )

            return content

        except Exception as exc:
            last_error = exc

            # Never retry a deterministic HTTP 400. Retrying the
            # same invalid request only wastes time.
            if (
                isinstance(exc, RuntimeError)
                and str(exc).startswith(
                    "LM Studio returned HTTP 400"
                )
            ):
                break

            if attempt == 0:
                time.sleep(1)

    raise RuntimeError(
        f"LLM generation failed: {last_error}"
    )


# ============================================================
# RESPONSE CLEANING
# ============================================================

def clean_response(
    text: str,
) -> str:
    """
    Clean common Gemma output wrappers.

    Does NOT generate or replace narrative.
    """

    if not text:
        return ""

    text = text.strip()

    text = re.sub(
        r"^```(?:text|markdown)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Sometimes small models return:
    #
    # {"response": "..."}
    #
    # Unwrap it if possible.
    if (
        text.startswith("{")
        and text.endswith("}")
    ):

        try:

            parsed = json.loads(text)

            if isinstance(
                parsed,
                dict,
            ):

                possible = (
                    parsed.get("response")
                    or parsed.get("content")
                    or parsed.get("text")
                )

                if possible:

                    text = str(
                        possible
                    )

        except Exception:
            pass

    return text.strip()


# ============================================================
# FACT EXTRACTION
# ============================================================

def _extract_kpi(
    analysis: Dict[str, Any],
    names: list[str],
) -> float:

    # Preferred clean percentage structure.
    kpi_percent = analysis.get(
        "kpi_percent",
        {},
    )

    if isinstance(
        kpi_percent,
        dict,
    ):

        for name in names:

            if name in kpi_percent:

                value = _safe_float(
                    _raw(
                        kpi_percent[name]
                    )
                )

                return value

    # verified_kpis
    verified_kpis = analysis.get(
        "verified_kpis",
        {},
    )

    if isinstance(
        verified_kpis,
        dict,
    ):

        for name in names:

            if name in verified_kpis:

                value = _safe_float(
                    _raw(
                        verified_kpis[name]
                    )
                )

                if (
                    name.lower()
                    != "oee"
                    and abs(value) <= 1
                ):
                    value *= 100

                return value

    # kpi section
    kpi = analysis.get(
        "kpi",
        {},
    )

    if isinstance(
        kpi,
        dict,
    ):

        for name in names:

            if name in kpi:

                value = _safe_float(
                    _raw(
                        kpi[name]
                    )
                )

                if (
                    name.lower()
                    != "oee"
                    and abs(value) <= 1
                ):
                    value *= 100

                return value

    # Recursive fallback.
    value = _find_nested_value(
        analysis,
        names,
    )

    if value is not None:

        value = _safe_float(
            _raw(value)
        )

        if (
            names[0].lower()
            != "oee"
            and abs(value) <= 1
        ):
            value *= 100

        return value

    return 0.0


def _extract_previous_kpi(
    previous_week: Any,
    names: list[str],
) -> Optional[float]:
    """
    Returns None (not 0.0) whenever there is no verified previous-week
    value to report. A missing previous week is NOT the same as a
    previous week that was actually 0% — treating the two the same
    caused every current-week KPI to be misreported as "decreased by
    its own full value week over week" whenever no prior run existed.
    Callers must handle None explicitly (e.g. "no data available"),
    never feed it straight into arithmetic or string formatting.
    """

    if not isinstance(
        previous_week,
        dict,
    ):
        return None

    for name in names:

        if name in previous_week:

            raw_value = _raw(
                previous_week[name]
            )

            if raw_value is None:
                return None

            value = _safe_float(raw_value)

            if (
                names[0].lower()
                != "oee"
                and abs(value) <= 1
            ):
                value *= 100

            return value

    return None


def _extract_quality_contributors(
    analysis: Dict[str, Any],
) -> list[Dict[str, Any]]:

    possible = (
        analysis.get(
            "quality_loss_contributors"
        )
        or analysis.get(
            "quality_loss_reasons"
        )
        or analysis.get(
            "quality_contributors"
        )
        or []
    )

    contributors = []

    if isinstance(
        possible,
        dict,
    ):

        for reason, value in possible.items():

            contributors.append(
                {
                    "reason": str(reason),
                    "bags": _safe_int(
                        _raw(value)
                    ),
                }
            )

    elif isinstance(
        possible,
        list,
    ):

        for item in possible:

            if not isinstance(
                item,
                dict,
            ):
                continue

            reason = (
                item.get("reason")
                or item.get("name")
                or item.get("label")
                or item.get("fault")
                or "Unknown"
            )

            bags = (
                item.get("bags")
                or item.get("count")
                or item.get("value")
                or item.get("quantity")
                or 0
            )

            contributors.append(
                {
                    "reason": str(reason),
                    "bags": _safe_int(
                        _raw(bags)
                    ),
                }
            )

    contributors = [
        item
        for item in contributors
        if item["bags"] > 0
    ]

    contributors.sort(
        key=lambda item: item["bags"],
        reverse=True,
    )

    return contributors


def _extract_downtime_contributors(
    analysis: Dict[str, Any],
) -> list[Dict[str, Any]]:

    contributors = []

    # --------------------------------------------------------
    # Preferred: report-facing downtime list.
    # --------------------------------------------------------

    possible = (
        analysis.get(
            "downtime_contributors"
        )
        or analysis.get(
            "downtime_faults"
        )
        or analysis.get(
            "fault_contributors"
        )
        or []
    )

    if isinstance(
        possible,
        dict,
    ):

        for reason, value in possible.items():

            minutes = _safe_float(
                _raw(value)
            )

            if minutes > 0:

                contributors.append(
                    {
                        "reason": str(reason),
                        "minutes": minutes,
                    }
                )

    elif isinstance(
        possible,
        list,
    ):

        for item in possible:

            if not isinstance(
                item,
                dict,
            ):
                continue

            # IMPORTANT:
            # Current pipeline uses "reason".
            reason = (
                item.get("reason")
                or item.get("name")
                or item.get("fault")
                or item.get("label")
                or item.get("description")
                or "Unknown"
            )

            minutes = (
                item.get("minutes")
                or item.get("duration")
                or item.get("loss_minutes")
                or item.get("value")
                or 0
            )

            minutes = _safe_float(
                _raw(minutes)
            )

            if minutes > 0:

                contributors.append(
                    {
                        "reason": str(reason),
                        "minutes": minutes,
                    }
                )

    # --------------------------------------------------------
    # If the report-facing list doesn't exist, reconstruct
    # downtime from availability_breakdown_by_plc /
    # downtime_breakdown_by_plc.
    # --------------------------------------------------------

    if not contributors:

        breakdown = (
            analysis.get(
                "downtime_breakdown_by_plc"
            )
            or analysis.get(
                "availability_breakdown_by_plc"
            )
            or {}
        )

        if isinstance(
            breakdown,
            dict,
        ):

            for plc_id, plc_entry in breakdown.items():

                if not isinstance(
                    plc_entry,
                    dict,
                ):
                    continue

                machine_name = (
                    plc_entry.get("machine")
                    or plc_id
                )

                for key, value in plc_entry.items():

                    if key == "machine":
                        continue

                    minutes = _safe_float(
                        _raw(value)
                    )

                    if minutes <= 0:
                        continue

                    label = str(key)

                    label = (
                        label
                        .replace("_min", "")
                        .replace("_", " ")
                        .strip()
                        .title()
                    )

                    contributors.append(
                        {
                            "reason":
                                f"{machine_name} {label}",

                            "minutes":
                                minutes,
                        }
                    )

    contributors.sort(
        key=lambda item: item["minutes"],
        reverse=True,
    )

    return contributors


def _extract_shift_changeover(
    analysis: Dict[str, Any],
) -> Dict[str, float]:

    data = analysis.get(
        "shift_changeover",
        {},
    )

    if not isinstance(
        data,
        dict,
    ):
        data = {}

    shift_a = _safe_float(
        data.get(
            "shift_a_cumulative_delay",
            data.get(
                "shift_a_delay",
                data.get(
                    "shift_a",
                    0,
                ),
            ),
        )
    )

    shift_b = _safe_float(
        data.get(
            "shift_b_cumulative_delay",
            data.get(
                "shift_b_delay",
                data.get(
                    "shift_b",
                    0,
                ),
            ),
        )
    )

    shift_c = _safe_float(
        data.get(
            "shift_c_cumulative_delay",
            data.get(
                "shift_c_delay",
                data.get(
                    "shift_c",
                    0,
                ),
            ),
        )
    )

    total = _safe_float(
        data.get(
            "total_minutes",
            data.get(
                "total_delay",
                0,
            ),
        )
    )

    if total <= 0:

        total = (
            shift_a
            + shift_b
            + shift_c
        )

    return {
        "shift_a_minutes": shift_a,
        "shift_b_minutes": shift_b,
        "shift_c_minutes": shift_c,
        "total_minutes": total,
    }


def extract_facts(
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Extract verified numerical facts from analysis.json.

    IMPORTANT:

    analysis.json is the numerical source of truth.

    This function does NOT create AI narrative.
    """

    if not isinstance(
        analysis,
        dict,
    ):

        fail(
            "analysis.json must contain a JSON object."
        )

    # ========================================================
    # IDENTIFIERS
    # ========================================================

    site_id = (
        analysis.get("site_id")
        or analysis.get("site")
        or analysis.get("site_name")
        or analysis.get("plant_site_name")
        or ""
    )

    week = (
        analysis.get("week")
        or analysis.get("report_week")
        or ""
    )

    machine = (
        analysis.get("machine")
        or analysis.get("machine_name")
        or analysis.get("machine_type")
        or analysis.get("equipment_line_name")
        or "FillPac"
    )

    # ========================================================
    # CURRENT KPIs
    # ========================================================

    availability = _extract_kpi(
        analysis,
        [
            "availability",
            "Availability",
        ],
    )

    performance = _extract_kpi(
        analysis,
        [
            "performance",
            "Performance",
        ],
    )

    quality = _extract_kpi(
        analysis,
        [
            "quality",
            "Quality",
        ],
    )

    oee = _extract_kpi(
        analysis,
        [
            "oee",
            "OEE",
        ],
    )

    # ========================================================
    # TARGET
    # ========================================================

    target_oee = _safe_float(
        _find_nested_value(
            analysis,
            [
                "target_oee",
                "Target OEE",
                "oee_target",
            ],
        ),
        85.0,
    )

    if target_oee <= 0:
        target_oee = 85.0

    # ========================================================
    # PREVIOUS WEEK
    # ========================================================

    previous_week = (
        analysis.get(
            "previous_week_kpis"
        )
        or analysis.get(
            "previous_kpi"
        )
        or analysis.get(
            "previous_week"
        )
        or {}
    )

    if not isinstance(
        previous_week,
        dict,
    ):
        previous_week = {}

    previous_oee = _extract_previous_kpi(
        previous_week,
        [
            "oee",
            "OEE",
        ],
    )

    previous_availability = _extract_previous_kpi(
        previous_week,
        [
            "availability",
            "Availability",
        ],
    )

    previous_performance = _extract_previous_kpi(
        previous_week,
        [
            "performance",
            "Performance",
        ],
    )

    previous_quality = _extract_previous_kpi(
        previous_week,
        [
            "quality",
            "Quality",
        ],
    )

    # True only if at least one previous-week KPI was actually found.
    # Drives whether build_data_block() is allowed to show the LLM
    # any week-over-week comparison at all.
    has_previous_week = any(
        value is not None
        for value in (
            previous_oee,
            previous_availability,
            previous_performance,
            previous_quality,
        )
    )

    # ========================================================
    # PRODUCTION
    # ========================================================

    performance_details = (
        analysis.get(
            "performance_details",
            {},
        )
    )

    if not isinstance(
        performance_details,
        dict,
    ):
        performance_details = {}

    # NOTE: analysis.json stores these as cell-info dicts,
    # e.g. {"raw_value": 123, "display_value": "123", ...} —
    # not plain numbers. _raw() unwraps that shape (and is a
    # no-op on plain numbers/strings), so it MUST wrap the
    # whole value before _safe_int() ever sees it. Without
    # this, _safe_float() stringifies the dict, fails to
    # parse it as a float, and silently returns 0 — which is
    # exactly the "0 bags" / "0.00%" bug this fixes.
    actual_good_bags = _safe_int(
        _raw(
            performance_details.get(
                "actual_good_bags",
                performance_details.get(
                    "good_bags",
                    _find_nested_value(
                        analysis,
                        [
                            "actual_good_bags",
                            "Actual Good Bags",
                        ],
                    ),
                ),
            )
        )
    )

    expected_good_bags = _safe_int(
        _raw(
            performance_details.get(
                "expected_good_bags",
                performance_details.get(
                    "expected_bags",
                    _find_nested_value(
                        analysis,
                        [
                            "expected_good_bags",
                            "Expected Good Bags",
                            "expected_bags",
                            "Expected Bags",
                        ],
                    ),
                ),
            )
        )
    )

    production_gap = max(
        expected_good_bags
        - actual_good_bags,
        0,
    )

    # ========================================================
    # QUALITY
    # ========================================================

    quality_details = analysis.get(
        "quality_details",
        {},
    )

    if not isinstance(
        quality_details,
        dict,
    ):
        quality_details = {}

    good_bags = _safe_int(
        _raw(
            quality_details.get(
                "good_bags",
                _find_nested_value(
                    analysis,
                    [
                        "good_bags",
                        "Good Bags",
                    ],
                ),
            )
        )
    )

    burst_bags = _safe_int(
        _raw(
            quality_details.get(
                "burst_bags",
                quality_details.get(
                    "burst",
                    _find_nested_value(
                        analysis,
                        [
                            "burst_bags",
                            "Burst Bags",
                        ],
                    ),
                ),
            )
        )
    )

    out_of_limit = _safe_int(
        _raw(
            quality_details.get(
                "out_of_limit_bags",
                quality_details.get(
                    "out_of_limit",
                    _find_nested_value(
                        analysis,
                        [
                            "out_of_limit_bags",
                            "out_of_limit",
                            "Out Of Limit",
                            "Out of Limit",
                        ],
                    ),
                ),
            )
        )
    )

    total_bad_bags = (
        burst_bags
        + out_of_limit
    )

    total_produced_bags = (
        good_bags
        + total_bad_bags
    )

    if total_produced_bags <= 0:

        total_produced_bags = _safe_int(
            _raw(
                _find_nested_value(
                    analysis,
                    [
                        "total_produced_bags",
                        "total_bags_produced",
                        "Total Produced Bags",
                    ],
                )
            )
        )

    quality_loss_percentage = (
        (
            total_bad_bags
            / total_produced_bags
            * 100
        )
        if total_produced_bags > 0
        else 0.0
    )

    quality_contributors = (
        _extract_quality_contributors(
            analysis
        )
    )

    # If explicit contributors are absent,
    # construct the two known quality-loss categories.
    if not quality_contributors:

        quality_contributors = []

        if out_of_limit > 0:

            quality_contributors.append(
                {
                    "reason":
                        "Out of Limit Bags",

                    "bags":
                        out_of_limit,
                }
            )

        if burst_bags > 0:

            quality_contributors.append(
                {
                    "reason":
                        "Burst Bags",

                    "bags":
                        burst_bags,
                }
            )

        quality_contributors.sort(
            key=lambda item: item["bags"],
            reverse=True,
        )

    # ========================================================
    # DOWNTIME
    # ========================================================

    availability_details = analysis.get(
        "availability_details",
        {},
    )

    if not isinstance(
        availability_details,
        dict,
    ):
        availability_details = {}

    fault_duration = _safe_float(
        _find_nested_value(
            analysis,
            [
                "fault_duration",
                "Fault duration",
            ],
        )
    )

    fault_scaled = _safe_float(
        _find_nested_value(
            analysis,
            [
                "fault_scaled",
                "Fault scaled",
            ],
        )
    )

    ideal_fault = _safe_float(
        _find_nested_value(
            analysis,
            [
                "ideal_fault",
                "Ideal fault",
            ],
        )
    )

    planned_production_time = _safe_float(
        availability_details.get(
            "planned_production_time_min",
            availability_details.get(
                "planned_time_min",
                _find_nested_value(
                    analysis,
                    [
                        "planned_production_time_min",
                        "planned_time_min",
                        "planned_production_time",
                    ],
                ),
            ),
        )
    )

    total_available_time = _safe_float(
        availability_details.get(
            "total_available_time_min",
            availability_details.get(
                "available_time_min",
                _find_nested_value(
                    analysis,
                    [
                        "total_available_time_min",
                        "available_time_min",
                    ],
                ),
            ),
        )
    )

    total_availability_loss = _safe_float(
        availability_details.get(
            "total_availability_loss_min",
            availability_details.get(
                "availability_loss_min",
                _find_nested_value(
                    analysis,
                    [
                        "total_availability_loss_min",
                        "availability_loss_min",
                    ],
                ),
            ),
        )
    )

    downtime_contributors = (
        _extract_downtime_contributors(
            analysis
        )
    )

    total_classified_downtime = sum(
        item["minutes"]
        for item
        in downtime_contributors
    )

    # Use explicitly calculated classified downtime
    # if present and larger than the report-facing list.
    classified_from_analysis = _safe_float(
        _find_nested_value(
            analysis,
            [
                "classified_downtime_min",
                "classified_downtime",
                "total_downtime",
            ],
        )
    )

    if classified_from_analysis > 0:

        total_classified_downtime = max(
            total_classified_downtime,
            classified_from_analysis,
        )

    # ========================================================
    # SHIFT CHANGEOVER
    # ========================================================

    shift_changeover = (
        _extract_shift_changeover(
            analysis
        )
    )

    # ========================================================
    # SPOUT DOWNTIME
    # ========================================================

    spout_downtime = (
        analysis.get(
            "spout_downtime"
        )
        or analysis.get(
            "spout_downtime_by_plc"
        )
        or {}
    )

    if not isinstance(
        spout_downtime,
        dict,
    ):
        spout_downtime = {}

    # ========================================================
    # STATUS
    # ========================================================

    oee_status = (
        "On Target"
        if oee >= target_oee
        else "Below Target"
    )

    # ========================================================
    # FACTS
    # ========================================================

    facts = {

        "site_id":
            site_id,

        "week":
            week,

        "machine":
            machine,

        "target_oee":
            target_oee,

        "kpis": {

            "availability":
                availability,

            "performance":
                performance,

            "quality":
                quality,

            "oee":
                oee,
        },

        "oee_status":
            oee_status,

        "previous_week": {

            "availability":
                previous_availability,

            "performance":
                previous_performance,

            "quality":
                previous_quality,

            "oee":
                previous_oee,
        },

        # Whether ANY verified previous-week KPI was found. When False,
        # every value in "previous_week" above is None (not 0) — build_data_block()
        # must not compute or print a week-over-week delta in that case.
        "has_previous_week":
            has_previous_week,

        "performance_details": {

            "actual_good_bags":
                actual_good_bags,

            "expected_good_bags":
                expected_good_bags,

            "production_gap":
                production_gap,
        },

        "quality_details": {

            "good_bags":
                good_bags,

            "burst_bags":
                burst_bags,

            "out_of_limit":
                out_of_limit,

            "total_bad_bags":
                total_bad_bags,

            "total_produced_bags":
                total_produced_bags,

            "quality_loss_percentage":
                quality_loss_percentage,

            "contributors":
                quality_contributors,
        },

        "downtime": {

            "fault_duration":
                fault_duration,

            "fault_scaled":
                fault_scaled,

            "ideal_fault":
                ideal_fault,

            "planned_production_time":
                planned_production_time,

            "total_available_time":
                total_available_time,

            "total_availability_loss":
                total_availability_loss,

            "total_classified_downtime":
                total_classified_downtime,

            "contributors":
                downtime_contributors,
        },

        "shift_changeover":
            shift_changeover,

        "spout_downtime":
            spout_downtime,
    }

    return facts


# ============================================================
# NUMBER FORMATTING
# ============================================================

def _format_number(
    value: Any,
    decimals: int = 2,
) -> str:

    number = _safe_float(value)

    if decimals == 0:

        return f"{number:.0f}"

    return f"{number:.{decimals}f}"


# ============================================================
# BUILD VERIFIED DATA BLOCK
# ============================================================

def build_data_block(
    facts: Dict[str, Any],
) -> str:
    """
    Build the exact verified data supplied to Gemma.

    No narrative is generated here.
    """

    kpis = facts["kpis"]

    previous = facts[
        "previous_week"
    ]

    has_previous_week = facts.get(
        "has_previous_week",
        False,
    )

    performance = facts[
        "performance_details"
    ]

    quality = facts[
        "quality_details"
    ]

    downtime = facts[
        "downtime"
    ]


    lines = []

    # ========================================================
    # IDENTIFIERS
    # ========================================================

    lines.append(
        "VERIFIED WEEKLY OEE DATA"
    )

    lines.append("")

    lines.append(
        f"Site: {facts['site_id']}"
    )

    lines.append(
        f"Report week: {facts['week']}"
    )

    lines.append(
        f"Machine: {facts['machine']}"
    )

    lines.append("")

    # ========================================================
    # CURRENT KPIs
    # ========================================================

    lines.append(
        "CURRENT KPI VALUES"
    )

    lines.append(
        f"Availability: "
        f"{_format_number(kpis['availability'])}%"
    )

    lines.append(
        f"Performance: "
        f"{_format_number(kpis['performance'])}%"
    )

    lines.append(
        f"Quality: "
        f"{_format_number(kpis['quality'])}%"
    )

    lines.append(
        f"OEE: "
        f"{_format_number(kpis['oee'])}%"
    )

    lines.append(
        f"Target OEE: "
        f"{_format_number(facts['target_oee'])}%"
    )

    lines.append(
        f"OEE status: "
        f"{facts['oee_status']}"
    )

    lines.append("")

    # ========================================================
    # KPI RANKING
    # ========================================================

    component_kpis = {

        "Availability":
            kpis["availability"],

        "Performance":
            kpis["performance"],

        "Quality":
            kpis["quality"],
    }

    weakest_value = min(
        component_kpis.values()
    )

    strongest_value = max(
        component_kpis.values()
    )

    weakest = [
        name
        for name, value
        in component_kpis.items()
        if abs(
            value - weakest_value
        ) < 0.01
    ]

    strongest = [
        name
        for name, value
        in component_kpis.items()
        if abs(
            value - strongest_value
        ) < 0.01
    ]

    lines.append(
        "KPI RANKING"
    )

    lines.append(
        "Weakest KPI: "
        + " and ".join(weakest)
    )

    lines.append(
        "Strongest KPI: "
        + " and ".join(strongest)
    )

    lines.append("")

    # ========================================================
    # PREVIOUS WEEK
    # ========================================================
    #
    # BUGFIX: previously this section always printed a number —
    # when no prior week's analysis.json existed, previous[name]
    # defaulted to 0.0, so a KPI of e.g. 29.95% got reported to the
    # LLM as "decreased by 29.95 percentage points from the previous
    # week", which is fabricated (there was no previous week to
    # compare against). We now only print previous-week values and
    # deltas when has_previous_week is True; otherwise we say so
    # explicitly so the LLM cannot infer a false comparison.

    def _prev_line(label: str, key: str) -> str:
        value = previous.get(key)
        if value is None:
            return f"{label}: NOT AVAILABLE (no verified previous-week value)"
        return f"{label}: {_format_number(value)}%"

    def _change_line(label: str, key: str) -> str:
        prev_value = previous.get(key)
        if prev_value is None:
            return (
                f"{label} change: NOT AVAILABLE — do not report a "
                f"week-over-week change for {label}."
            )
        return (
            f"{label} change: "
            f"{kpis[key] - prev_value:+.2f} percentage points"
        )

    lines.append(
        "PREVIOUS WEEK KPI VALUES"
    )

    if has_previous_week:

        lines.append(_prev_line("Availability", "availability"))
        lines.append(_prev_line("Performance", "performance"))
        lines.append(_prev_line("Quality", "quality"))
        lines.append(_prev_line("OEE", "oee"))

    else:

        lines.append(
            "NOT AVAILABLE — no verified previous-week analysis "
            "exists for this site. Do not state or imply any "
            "specific previous-week value."
        )

    lines.append("")

    # ========================================================
    # WEEK OVER WEEK CHANGES
    # ========================================================

    lines.append(
        "CALCULATED WEEK-OVER-WEEK CHANGES"
    )

    if has_previous_week:

        lines.append(_change_line("Availability", "availability"))
        lines.append(_change_line("Performance", "performance"))
        lines.append(_change_line("Quality", "quality"))
        lines.append(_change_line("OEE", "oee"))

    else:

        lines.append(
            "NOT AVAILABLE — no previous week to compare against. "
            "Do NOT report a week-over-week change, increase, or "
            "decrease for any KPI. In WEEK-OVER-WEEK TREND, state "
            "plainly that no prior week's verified data exists yet "
            "for comparison, and describe only how the current "
            "values compare to target."
        )

    lines.append("")

    # ========================================================
    # PERFORMANCE
    # ========================================================

    lines.append(
        "PRODUCTION / PERFORMANCE DATA"
    )

    lines.append(
        f"Actual good bags: "
        f"{performance['actual_good_bags']:,}"
    )

    lines.append(
        f"Expected good bags: "
        f"{performance['expected_good_bags']:,}"
    )

    lines.append(
        f"Production gap: "
        f"{performance['production_gap']:,}"
    )

    lines.append("")

    # ========================================================
    # QUALITY
    # ========================================================

    lines.append(
        "QUALITY DATA"
    )

    lines.append(
        f"Good bags: "
        f"{quality['good_bags']:,}"
    )

    lines.append(
        f"Burst bags: "
        f"{quality['burst_bags']:,}"
    )

    lines.append(
        f"Out-of-limit bags: "
        f"{quality['out_of_limit']:,}"
    )

    lines.append(
        f"Total bad bags: "
        f"{quality['total_bad_bags']:,}"
    )

    lines.append(
        f"Total produced bags: "
        f"{quality['total_produced_bags']:,}"
    )

    lines.append(
        f"Quality loss: "
        f"{quality['quality_loss_percentage']:.2f}%"
    )

    if quality["contributors"]:

        lines.append(
            "Quality loss contributors:"
        )

        total_quality_loss = sum(
            item["bags"]
            for item
            in quality["contributors"]
        )

        for item in quality[
            "contributors"
        ][:MAX_QUALITY_POINTS]:

            bags = item["bags"]

            share = (
                (
                    bags
                    / total_quality_loss
                    * 100
                )
                if total_quality_loss > 0
                else 0
            )

            lines.append(
                f"- {item['reason']}: "
                f"{bags:,} bags "
                f"({share:.2f}% of recorded quality loss)"
            )

    else:

        lines.append(
            "Quality loss contributors: "
            "No detailed reason breakdown supplied."
        )

    lines.append("")

    # ========================================================
    # DOWNTIME
    # ========================================================

    lines.append(
        "DOWNTIME DATA"
    )

    lines.append(
        f"Fault duration: "
        f"{_format_number(downtime['fault_duration'])}"
    )

    lines.append(
        f"Fault scaled: "
        f"{_format_number(downtime['fault_scaled'])}"
    )

    lines.append(
        f"Ideal fault: "
        f"{_format_number(downtime['ideal_fault'])}"
    )

    lines.append(
        f"Planned production time: "
        f"{_format_number(downtime['planned_production_time'])} minutes"
    )

    lines.append(
        f"Total available time: "
        f"{_format_number(downtime['total_available_time'])} minutes"
    )

    lines.append(
        f"Total availability loss: "
        f"{_format_number(downtime['total_availability_loss'])} minutes"
    )

    lines.append(
        f"Total classified downtime: "
        f"{_format_number(downtime['total_classified_downtime'])} minutes"
    )

    lines.append("")

    if downtime["contributors"]:

        lines.append(
            "DOWNTIME CONTRIBUTORS"
        )

        total_downtime = (
            downtime[
                "total_classified_downtime"
            ]
        )

        if total_downtime <= 0:

            total_downtime = sum(
                item["minutes"]
                for item
                in downtime["contributors"]
            )

        for item in downtime[
            "contributors"
        ][:MAX_DOWNTIME_POINTS]:

            minutes = item[
                "minutes"
            ]

            share = (
                (
                    minutes
                    / total_downtime
                    * 100
                )
                if total_downtime > 0
                else 0
            )

            lines.append(
                f"- {item['reason']}: "
                f"{minutes:.2f} minutes "
                f"({share:.2f}% of classified downtime)"
            )

    else:

        lines.append(
            "No downtime contributor "
            "breakdown supplied."
        )

    lines.append("")

    # ========================================================
    # SHIFT CHANGEOVER
    # ========================================================
    #
    # INTENTIONALLY NOT SENT TO GEMMA.
    #
    # Shift changeover data remains in facts and in the final
    # insights.json for compatibility/audit purposes, but the
    # user explicitly requires that it must not be included in
    # the LLM prompt or used to generate AI insights.
    #
    # ========================================================
    # SPOUT DATA
    # ========================================================

    if facts["spout_downtime"]:

        lines.append(
            "SPOUT DOWNTIME DATA"
        )

        lines.append(
            json.dumps(
                facts["spout_downtime"],
                ensure_ascii=False,
            )
        )

    else:

        lines.append(
            "SPOUT DOWNTIME DATA"
        )

        lines.append(
            "No spout downtime data supplied."
        )

    return "\n".join(lines)


# ============================================================
# REQUIRED AI SECTIONS
# ============================================================

REQUIRED_SECTIONS = [

    "EXECUTIVE SUMMARY",

    "MAIN LOSS DRIVER",

    "PERFORMANCE CONCERN",

    "STRONGEST KPI",

    "WEEK-OVER-WEEK TREND",

    "QUALITY SUMMARY",

    "MAIN QUALITY ISSUE",

    "KEY DECISION POINT",

    "MISSING DATA",
]


# ============================================================
# LLM SECTION PARSER
# ============================================================

def extract_sections(
    text: str,
) -> Dict[str, str]:
    """
    Extract the nine required sections from Gemma output.

    Handles all of these common small-model formats:

        EXECUTIVE SUMMARY:
        EXECUTIVE SUMMARY
        ## EXECUTIVE SUMMARY
        **EXECUTIVE SUMMARY:**
        **MAIN LOSS DRIVER:** Some narrative on the same line

    The parser deliberately accepts markdown formatting around the
    section heading, but does not alter the narrative itself.
    """

    if not text:
        return {}

    text = (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    ).strip()

    # A section header may be bolded, a markdown heading, or plain text.
    # Content may begin on the same line as the header.
    section_pattern = "|".join(
        re.escape(section)
        for section in REQUIRED_SECTIONS
    )

    header_re = re.compile(
        rf"(?im)^\s*"
        rf"(?:#{{1,6}}\s*)?"
        rf"(?:\*\*\s*)?"
        rf"(?P<section>{section_pattern})"
        rf"(?:\s*\*\*)?"
        rf"\s*:?\s*"
        rf"(?:\*\*)?"
        rf"(?P<inline>.*?)"
        rf"\s*$"
    )

    matches = list(header_re.finditer(text))

    sections: Dict[str, str] = {}

    for index, match in enumerate(matches):

        section = match.group("section").strip()

        # Preserve text written after the heading on the same line.
        inline_body = (
            match.group("inline") or ""
        ).strip()

        if index + 1 < len(matches):
            next_start = matches[index + 1].start()
        else:
            next_start = len(text)

        remaining_body = text[
            match.end():next_start
        ].strip()

        if inline_body and remaining_body:
            body = f"{inline_body}\n{remaining_body}"
        else:
            body = inline_body or remaining_body

        # Remove separator lines without touching normal narrative.
        body = re.sub(
            r"(?m)^\s*[-_=]{3,}\s*$",
            "",
            body,
        ).strip()

        # Remove wrapping bold markers that a model may leave around
        # the entire extracted section body.
        if (
            body.startswith("**")
            and body.endswith("**")
            and body.count("**") == 2
        ):
            body = body[2:-2].strip()

        if body:
            sections[section] = body

    return sections


def _section(
    sections: Dict[str, str],
    name: str,
) -> str:

    value = sections.get(
        name,
        "",
    )

    return str(
        value
    ).strip()


# ============================================================
# BUILD INSIGHTS JSON
# ============================================================

def build_insights(
    facts: Dict[str, Any],
    raw_response: str,
) -> Dict[str, Any]:
    """
    Convert Gemma's response into insights.json.

    IMPORTANT:

    There is NO deterministic narrative fallback.

    If Gemma fails to produce a required narrative section,
    report generation stops.
    """

    sections = extract_sections(
        raw_response
    )

    emit_message(
        "[LLM] Parsed sections: "
        + ", ".join(sections.keys())
    )

    executive_summary = _section(
        sections,
        "EXECUTIVE SUMMARY",
    )

    main_loss_driver = _section(
        sections,
        "MAIN LOSS DRIVER",
    )

    performance_concern = _section(
        sections,
        "PERFORMANCE CONCERN",
    )

    strongest_kpi = _section(
        sections,
        "STRONGEST KPI",
    )

    week_over_week_trend = _section(
        sections,
        "WEEK-OVER-WEEK TREND",
    )

    quality_summary = _section(
        sections,
        "QUALITY SUMMARY",
    )

    main_quality_issue = _section(
        sections,
        "MAIN QUALITY ISSUE",
    )

    key_decision_point = _section(
        sections,
        "KEY DECISION POINT",
    )

    missing_data = _section(
        sections,
        "MISSING DATA",
    )

    # ========================================================
    # REQUIRED FIELD VALIDATION
    # ========================================================

    required = {

        "executive_summary":
            executive_summary,

        "main_loss_driver":
            main_loss_driver,

        "performance_concern":
            performance_concern,

        "strongest_kpi":
            strongest_kpi,

        "week_over_week_trend":
            week_over_week_trend,

        "quality_summary":
            quality_summary,

        "main_quality_issue":
            main_quality_issue,

        "key_decision_point":
            key_decision_point,

        "missing_data":
            missing_data,
    }

    missing = [

        key
        for key, value
        in required.items()
        if not value
    ]

    if missing:

        raise RuntimeError(
            "The LLM response is missing required "
            "narrative sections:\n"
            + "\n".join(
                f"  - {item}"
                for item in missing
            )
            + "\n\n"
            "Raw LLM response:\n"
            "--------------------------------------------------\n"
            f"{raw_response}\n"
            "--------------------------------------------------"
        )

    # ========================================================
    # VALIDATE / NORMALIZE strongest_kpi SECTION
    # ========================================================
    #
    # Gemma 2B can occasionally ignore the explicit instruction
    # and write phrases such as "Quality ... OEE value ...".
    # Because analysis.json is the numerical source of truth,
    # we do not allow that model error to reach generate_docx.py.
    #
    # Only this one section is deterministically normalized.
    # All other narrative remains the model's generated text.

    avail_val = facts["kpis"]["availability"]
    perf_val = facts["kpis"]["performance"]
    qual_val = facts["kpis"]["quality"]

    kpi_values = {
        "Availability": avail_val,
        "Performance": perf_val,
        "Quality": qual_val,
    }

    max_kpi_value = max(kpi_values.values())

    strongest_kpi_names = [
        name
        for name, val in kpi_values.items()
        if abs(val - max_kpi_value) < 0.01
    ]

    strongest_kpi_text_lower = strongest_kpi.lower()

    # Valid component-KPI names that actually appear in the
    # verified strongest set.
    mentions_valid_strongest = any(
        re.search(
            rf"\b{re.escape(name.lower())}\b",
            strongest_kpi_text_lower,
        )
        for name in strongest_kpi_names
    )

    has_invalid_oee_wording = (
        "oee value" in strongest_kpi_text_lower
        or "oee of" in strongest_kpi_text_lower
        or "oee is the strongest" in strongest_kpi_text_lower
        or "strongest oee" in strongest_kpi_text_lower
    )

    claims_tie = (
        "tied" in strongest_kpi_text_lower
        or re.search(
            r"\b(availability|performance|quality)\s+and\s+"
            r"(availability|performance|quality)\b",
            strongest_kpi_text_lower,
        )
        is not None
    )

    invalid_strongest_section = (
        not mentions_valid_strongest
        or has_invalid_oee_wording
        or (
            claims_tie
            and len(strongest_kpi_names) == 1
        )
    )

    if invalid_strongest_section:
        if len(strongest_kpi_names) == 1:
            strongest_name = strongest_kpi_names[0]
            strongest_value = kpi_values[strongest_name]
            strongest_kpi = (
                f"{strongest_name} is the strongest KPI at "
                f"{strongest_value:.2f}%"
            )
        else:
            names_text = " and ".join(strongest_kpi_names)
            strongest_value = kpi_values[strongest_kpi_names[0]]
            strongest_kpi = (
                f"{names_text} are tied as the strongest KPIs at "
                f"{strongest_value:.2f}%"
            )

        emit_message(
            "[LLM] strongest_kpi narrative was invalid for the "
            "verified KPI ranking. It has been normalized from "
            "analysis.json so the report cannot contain an "
            "incorrect 'OEE value' statement."
        )

    # ========================================================
    # VERIFIED NUMBERS
    # ========================================================

    verified_kpis = {

        "availability":
            facts["kpis"]["availability"],

        "performance":
            facts["kpis"]["performance"],

        "quality":
            facts["kpis"]["quality"],

        "oee":
            facts["kpis"]["oee"],
    }

    # Status is derived only from verified numbers.
    verified_status = {}

    for key, value in verified_kpis.items():

        if key == "oee":

            target = facts[
                "target_oee"
            ]

            verified_status[key] = (
                "On Target"
                if value >= target
                else "Below Target"
            )

        else:

            verified_status[key] = (
                "good"
                if value >= 85
                else
                "warning"
                if value >= 70
                else
                "critical"
            )

    # ========================================================
    # FINAL RESULT
    # ========================================================

    result = {

        # ----------------------------------------------------
        # IDENTIFIERS
        # ----------------------------------------------------

        "site_id":
            facts["site_id"],

        "report_week":
            facts["week"],

        "machine":
            facts["machine"],

        # ----------------------------------------------------
        # AI NARRATIVE
        # ----------------------------------------------------

        "ai_insights": {

            "executive_summary":
                executive_summary,

            "main_loss_driver":
                main_loss_driver,

            "performance_concern":
                performance_concern,

            "strongest_kpi":
                strongest_kpi,

            "week_over_week_trend":
                week_over_week_trend,

            "quality_summary":
                quality_summary,

            "main_quality_issue":
                main_quality_issue,

            "key_decision_point":
                key_decision_point,

            "missing_data":
                missing_data,
        },

        # ----------------------------------------------------
        # FLAT AI FIELDS
        #
        # Kept intentionally because generate_docx.py may
        # access either ai_insights[...] or the top level.
        # ----------------------------------------------------

        "executive_summary":
            executive_summary,

        "main_loss_driver":
            main_loss_driver,

        "performance_concern":
            performance_concern,

        "strongest_kpi":
            strongest_kpi,

        "week_over_week_trend":
            week_over_week_trend,

        "quality_summary":
            quality_summary,

        "main_quality_issue":
            main_quality_issue,

        "key_decision_point":
            key_decision_point,

        "missing_data":
            missing_data,

        # ----------------------------------------------------
        # VERIFIED NUMBERS
        # ----------------------------------------------------

        "verified_kpis":
            verified_kpis,

        "verified_status":
            verified_status,

        "verified_performance":
            facts["performance_details"],

        "verified_quality":
            facts["quality_details"],

        "verified_downtime":
            facts["downtime"],

        "verified_shift_changeover":
            facts["shift_changeover"],

        "previous_week":
            facts["previous_week"],

        "target_oee":
            facts["target_oee"],

        # ----------------------------------------------------
        # AUDIT
        # ----------------------------------------------------

        "raw_model_response":
            raw_response,
    }

    return result


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def generate_insights(
    analysis=None,
    site=None,
    week=None,
    config_manager=None,
    status_callback=None,
    site_id=None,
    write_output=True,
    progress_callback=None,
):
    """
    Generate LLM insights for one site/week.

    This function intentionally supports both:

        generate_insights(
            analysis=...,
            site=...,
            week=...,
            config_manager=...,
            write_output=True,
        )

    and:

        generate_insights(
            site_id=...,
            week=...,
            config_manager=...,
            write_output=True,
        )

    `write_output` is required for compatibility with the
    current run_pipeline.py.
    """

    # ========================================================
    # NORMALIZE SITE
    # ========================================================

    if site_id is None:
        site_id = site

    if site_id is None:

        raise ValueError(
            "generate_insights() requires "
            "site= or site_id=."
        )

    if week is None:

        raise ValueError(
            "generate_insights() requires week=."
        )

    if config_manager is None:

        raise ValueError(
            "generate_insights() requires "
            "config_manager=."
        )

    emit_message(
        f"[{site_id}] Checking LM Studio connection...",
        status_callback,
        progress_callback,
    )

    selected_model = check_lm_studio()

    # ========================================================
    # RESOLVE PATHS
    # ========================================================

    paths = config_manager.get_paths(
        site_id,
        week,
    )

    analysis_path = Path(
        paths["analysis_json"]
    )

    # ConfigManager versions may not expose an explicit
    # "insights_json" path. The insights file must always live
    # beside the analysis.json for the same site/week.
    insights_path_value = paths.get("insights_json")

    if insights_path_value:
        insights_path = Path(insights_path_value)
    else:
        insights_path = analysis_path.parent / "insights.json"

    # ========================================================
    # LOAD ANALYSIS
    # ========================================================

    if analysis is None:

        if not analysis_path.exists():

            raise FileNotFoundError(
                "Analysis file not found:\n"
                f"{analysis_path}"
            )

        with open(
            analysis_path,
            "r",
            encoding="utf-8",
        ) as f:

            analysis = json.load(f)

    if not isinstance(
        analysis,
        dict,
    ):

        raise RuntimeError(
            "analysis.json must contain "
            "a JSON object."
        )

    # ========================================================
    # EXTRACT FACTS
    # ========================================================

    facts = extract_facts(
        analysis
    )

    # Pipeline arguments are authoritative.
    facts["site_id"] = site_id
    facts["week"] = week

    # ========================================================
    # BUILD VERIFIED DATA BLOCK
    # ========================================================

    data_block = build_data_block(
        facts
    )

    # ========================================================
    # BUILD PROMPT
    # ========================================================

    # IMPORTANT:
    #
    # prompts.py defines:
    #
    # build_prompt(system_prompt, user_data)
    #
    # The previous client incorrectly called:
    #
    # build_prompt(data_block)
    #
    # which would fail.
    #
    prompt = build_prompt(
        REPORT_SYSTEM_PROMPT,
        data_block,
    )

    emit_message(
        f"[{site_id}] Generating AI insights "
        f"for week {week}...",
        status_callback,
        progress_callback,
    )

    # ========================================================
    # CALL GEMMA
    # ========================================================

    raw_response = generate_text(
        prompt,
        REPORT_SYSTEM_PROMPT,
        selected_model,
    )

    cleaned_response = clean_response(
        raw_response
    )

    # ========================================================
    # BUILD / VALIDATE INSIGHTS
    # ========================================================

    result = build_insights(
        facts,
        cleaned_response,
    )

    # ========================================================
    # SAVE
    # ========================================================

    if write_output:

        insights_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with open(
            insights_path,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                result,
                f,
                indent=4,
                ensure_ascii=False,
            )

        emit_message(
            f"[{site_id}]   Fresh insights.json saved:",
            status_callback,
            progress_callback,
        )

        emit_message(
            f"              {insights_path}",
            status_callback,
            progress_callback,
        )

    else:

        emit_message(
            f"[{site_id}]   AI insights generated "
            f"(write_output=False)",
            status_callback,
            progress_callback,
        )

    return result


# ============================================================
# DIRECT TEST
# ============================================================

if __name__ == "__main__":

    print(
        "local_llm_client.py"
    )

    print(
        "This module is intended to be called "
        "from run_pipeline.py."
    )