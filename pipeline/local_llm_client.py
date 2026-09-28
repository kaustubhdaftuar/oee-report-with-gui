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

TEMPERATURE = 0.2

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

def check_lm_studio() -> None:
    """
    Verify that the LM Studio local server is reachable.
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
            f"Expected server:\n"
            f"{LM_STUDIO_URL}\n\n"
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


def generate_text(
    prompt: str,
    system_prompt: Optional[str] = None,
) -> str:
    """
    Send the verified report data to Gemma through LM Studio.
    """

    if system_prompt is None:

        system_prompt = REPORT_SYSTEM_PROMPT

    payload = {
        "model": MODEL_NAME,

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

            response.raise_for_status()

            data = response.json()

            choices = data.get(
                "choices",
                [],
            )

            if not choices:

                raise RuntimeError(
                    "LM Studio returned no choices."
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
                    "LM Studio returned an empty response."
                )

            return content

        except Exception as exc:

            last_error = exc

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
) -> float:

    if not isinstance(
        previous_week,
        dict,
    ):
        return 0.0

    for name in names:

        if name in previous_week:

            value = _safe_float(
                _raw(
                    previous_week[name]
                )
            )

            if (
                names[0].lower()
                != "oee"
                and abs(value) <= 1
            ):
                value *= 100

            return value

    return 0.0


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

    actual_good_bags = _safe_int(
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

    expected_good_bags = _safe_int(
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

    production_gap = (
        expected_good_bags
        - actual_good_bags
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

    burst_bags = _safe_int(
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

    out_of_limit = _safe_int(
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
            _find_nested_value(
                analysis,
                [
                    "total_produced_bags",
                    "total_bags_produced",
                    "Total Produced Bags",
                ],
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

    performance = facts[
        "performance_details"
    ]

    quality = facts[
        "quality_details"
    ]

    downtime = facts[
        "downtime"
    ]

    shift = facts[
        "shift_changeover"
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

    lines.append(
        "PREVIOUS WEEK KPI VALUES"
    )

    lines.append(
        f"Availability: "
        f"{_format_number(previous['availability'])}%"
    )

    lines.append(
        f"Performance: "
        f"{_format_number(previous['performance'])}%"
    )

    lines.append(
        f"Quality: "
        f"{_format_number(previous['quality'])}%"
    )

    lines.append(
        f"OEE: "
        f"{_format_number(previous['oee'])}%"
    )

    lines.append("")

    # ========================================================
    # WEEK OVER WEEK CHANGES
    # ========================================================

    lines.append(
        "CALCULATED WEEK-OVER-WEEK CHANGES"
    )

    lines.append(
        f"Availability change: "
        f"{kpis['availability'] - previous['availability']:+.2f} "
        f"percentage points"
    )

    lines.append(
        f"Performance change: "
        f"{kpis['performance'] - previous['performance']:+.2f} "
        f"percentage points"
    )

    lines.append(
        f"Quality change: "
        f"{kpis['quality'] - previous['quality']:+.2f} "
        f"percentage points"
    )

    lines.append(
        f"OEE change: "
        f"{kpis['oee'] - previous['oee']:+.2f} "
        f"percentage points"
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

    lines.append(
        "SHIFT CHANGEOVER DATA"
    )

    lines.append(
        f"Shift A delay: "
        f"{shift['shift_a_minutes']:.2f} minutes"
    )

    lines.append(
        f"Shift B delay: "
        f"{shift['shift_b_minutes']:.2f} minutes"
    )

    lines.append(
        f"Shift C delay: "
        f"{shift['shift_c_minutes']:.2f} minutes"
    )

    lines.append(
        f"Total shift changeover delay: "
        f"{shift['total_minutes']:.2f} minutes"
    )

    lines.append("")

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

    check_lm_studio()

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