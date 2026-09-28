"""
CORRECTED OEE ANALYSIS PIPELINE - create_analysis.py (Multi-site Support)

Fixes applied:
1. Proper date range: Monday 06:00 → Monday 06:00 (exactly 7 days)
2. Correct availability formula: (1 - (fault_time / 608.33)) * 100
3. Consistent output paths: output/<site>/<week>/report.xlsx and analysis.json
4. Elasticsearch runtime_mappings for scaledDurationPrevEvent
5. Nested sum aggregation in downtime queries
6. Multi-site support with configurable mappings
7. run_analysis_pipeline() now returns the same nested "analysis" dict that
   gets written to analysis.json — previously it returned the flat internal
   "metrics" dict instead, so anything consuming the in-memory return value
   (e.g. Stage 1.5's generate_insights()) saw a completely different shape
   than what was actually saved to disk.

Site Configurations Included:
- jk_cement_aligarh (JK Cement, Aligarh)
- shree_cement_etah (Shree Cement, Etah)

Easy to add more sites by adding to SITE_CONFIG dictionary.
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Any, Tuple

import pandas as pd
from elasticsearch import Elasticsearch




# ════════════════════════════════════════════════════════════════════════════
# SHIFT DELIMITER CALCULATION
# ════════════════════════════════════════════════════════════════════════════

def calculate_shift_delimiters(ist_datetime):
    """
    Calculate shift delimiters for a given datetime in IST.

    Shift A: 06:00 to 14:00
    Shift B: 14:00 to 22:00
    Shift C: 22:00 to 06:00 (next day)

    Returns:
        prodDayStart: Start of the production day (06:00)
        shiftBStart: Start of Shift B (14:00)
        shiftCStart: Start of Shift C (22:00)
    """
    ist = ZoneInfo("Asia/Kolkata")
    hour = ist_datetime.hour

    # Determine production day start (always at 06:00)
    if hour < 6:
        # Before 06:00, production day started yesterday
        prod_day_start = ist_datetime.replace(
            hour=6, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)
    else:
        # At or after 06:00, production day started today
        prod_day_start = ist_datetime.replace(
            hour=6, minute=0, second=0, microsecond=0
        )

    shift_b_start = prod_day_start + timedelta(hours=8)
    shift_c_start = prod_day_start + timedelta(hours=16)

    return prod_day_start, shift_b_start, shift_c_start


def get_shift_name(ist_datetime, prod_day_start, shift_b_start, shift_c_start):
    """
    Determine which shift a timestamp belongs to.
    """
    if ist_datetime >= prod_day_start and ist_datetime < shift_b_start:
        return "Shift A"
    elif ist_datetime >= shift_b_start and ist_datetime < shift_c_start:
        return "Shift B"
    else:
        return "Shift C"

# ════════════════════════════════════════════════════════════════════════════
# SITE CONFIGURATIONS - ADD NEW SITES HERE
# ════════════════════════════════════════════════════════════════════════════

SITE_CONFIG = {
    "jk_cement_aligarh": {
        "name": "JK Cement Aligarh",
        "location": "Aligarh, India",
        "es_index": "iotgateway-jkcement-aligarh-*",
        "plc_config": [
            {
                "plc_id": "PLC_01",
                "name": "Fillpac 1",
                "spouts": 16,
                "description": "Packer 1"
            },
            {
                "plc_id": "PLC_02",
                "name": "Fillpac 2",
                "spouts": 16,
                "description": "Packer 2"
            }
        ],
        "sensor_keywords": {
            "availability": {
                "fault_counter": {
                    "sensoridPrevEvent_keyword": "Fault_Counter",
                    "field": "durationPrevEvent"
                },
                "ideal_fault": {
                    "sensoridPrevEvent_keyword": "IdealFaultopen",
                    "field": "durationPrevEvent"
                }
            },
            "downtime_by_plc": {
                "PLC_01": {
                    "parentid_filter": "PLC_01",
                    "faults": {
                        "Spout Fault": {
                            "sensorid_keyword": "FAULT_OPEN_SP*",
                            "type": "wildcard"
                        },
                        "Main Drive Stop": {
                            "sensorid_keyword": "FillPackMainDrivestop",
                            "type": "term"
                        },
                        "Belt Not Running": {
                            "sensorid_keyword": "IdealFaultopen",
                            "type": "term"
                        }
                    }
                },
                "PLC_02": {
                    "parentid_filter": "PLC_02",
                    "faults": {
                        "Spout Fault": {
                            "sensorid_keyword": "FAULT_OPEN_P02_SP*",
                            "type": "wildcard"
                        },
                        "Main Drive Stop": {
                            "sensorid_keyword": "FillPackMainDrivestop",
                            "type": "term"
                        },
                        "Belt Not Running": {
                            "sensorid_keyword": "IdealFaultopen",
                            "type": "term"
                        }
                    }
                }
            },
            "quality": {
                "good_bags": {
                    "sensorid_keywords": ["Bag_Discharge_SP*", "Packer02_Bag_Discharge_SP*"],
                    "field": "value",
                    "type": "wildcard"
                },
                "burst_bags": {
                    "sensorid_keywords": ["DownTime_BagBurstFault_SP*", "DownTime_BagBurstFault_P02_SP*"],
                    "field": "value",
                    "type": "wildcard"
                },
                "out_of_limit_bags": {
                    "sensorid_keywords": ["DownTime_Bag_OutOfLimit_SP*", "DownTime_Bag_OutOfLimit_P02_SP*"],
                    "field": "value",
                    "type": "wildcard"
                }
            }
        }
    },

    "shree_cement_etah": {
        "name": "Shree Cement Etah",
        "location": "Etah, India",
        "es_index": "iotgateway-shreecement-etah-*",
        "plc_config": [
            {
                "plc_id": "PLC_01",
                "name": "Fillpac 1",
                "spouts": 16,
                "description": "Packer 1"
            },
            {
                "plc_id": "PLC_02",
                "name": "Fillpac 2",
                "spouts": 16,
                "description": "Packer 2"
            },
            {
                "plc_id": "PLC_03",
                "name": "Fillpac 3",
                "spouts": 16,
                "description": "Packer 3"
            },
            {
                "plc_id": "PLC_04",
                "name": "Fillpac 4",
                "spouts": 16,
                "description": "Packer 4"
            }
        ],
        "sensor_keywords": {
            "availability": {
                "fault_counter": {
                    "sensoridPrevEvent_keyword": "Fault_Counter",
                    "field": "durationPrevEvent"
                },
                "ideal_fault": {
                    "sensoridPrevEvent_keyword": "IdealFaultopen",
                    "field": "durationPrevEvent"
                }
            },
            "downtime_by_plc": {
                "PLC_01": {
                    "parentid_filter": "PLC_01",
                    "faults": {
                        "Spout Fault": {
                            "sensorid_keyword": "FAULT_OPEN_SP*",
                            "type": "wildcard"
                        },
                        "Main Drive Stop": {
                            "sensorid_keyword": "FillPackMainDrivestop",
                            "type": "term"
                        },
                        "Belt Not Running": {
                            "sensorid_keyword": "IdealFaultopen",
                            "type": "term"
                        }
                    }
                },
                "PLC_02": {
                    "parentid_filter": "PLC_02",
                    "faults": {
                        "Spout Fault": {
                            "sensorid_keyword": "PACKER02_FAULT_OPEN_SP*",
                            "type": "wildcard"
                        },
                        "Main Drive Stop": {
                            "sensorid_keyword": "FillPackMainDrivestop",
                            "type": "term"
                        },
                        "Belt Not Running": {
                            "sensorid_keyword": "IdealFaultopen",
                            "type": "term"
                        }
                    }
                },
                "PLC_03": {
                    "parentid_filter": "PLC_03",
                    "faults": {
                        "Spout Fault": {
                            "sensorid_keyword": "FAULT_OPEN_SP*",
                            "type": "wildcard"
                        },
                        "Main Drive Stop": {
                            "sensorid_keyword": "FillPackMainDrivestop",
                            "type": "term"
                        },
                        "Belt Not Running": {
                            "sensorid_keyword": "IdealFaultopen",
                            "type": "term"
                        }
                    }
                },
                "PLC_04": {
                    "parentid_filter": "PLC_04",
                    "faults": {
                        "Spout Fault": {
                            "sensorid_keyword": "PACKER02_FAULT_OPEN_SP*",
                            "type": "wildcard"
                        },
                        "Main Drive Stop": {
                            "sensorid_keyword": "FillPackMainDrivestop",
                            "type": "term"
                        },
                        "Belt Not Running": {
                            "sensorid_keyword": "IdealFaultopen",
                            "type": "term"
                        }
                    }
                }
            },
            "quality": {
                "good_bags": {
                    "sensorid_keywords": ["Bag_Discharge_SP*", "Packer02_Bag_Discharge_SP*"],
                    "field": "value",
                    "type": "wildcard"
                },
                "burst_bags": {
                    "sensorid_keywords": ["DownTime_BagBurstFault_SP*", "DownTime_BagBurstFault_P02_SP*"],
                    "field": "value",
                    "type": "wildcard"
                },
                "out_of_limit_bags": {
                    "sensorid_keywords": ["DownTime_Bag_OutOfLimit_SP*", "DownTime_Bag_OutOfLimit_P02_SP*"],
                    "field": "value",
                    "type": "wildcard"
                }
            }
        }
    }
}


def get_site_config(site_id: str) -> Dict[str, Any]:
    """Get configuration for a specific site."""
    if site_id not in SITE_CONFIG:
        raise ValueError(f"Unknown site: {site_id}. Available sites: {list(SITE_CONFIG.keys())}")
    return SITE_CONFIG[site_id]


# ════════════════════════════════════════════════════════════════════════════
# FIX #1: PROPER DATE RANGE CALCULATION (Monday 06:00 → Monday 06:00)
# ════════════════════════════════════════════════════════════════════════════

def get_monday_6am_for_week(week: int, year: int = None) -> Tuple[datetime, datetime]:
    """
    Returns Monday 06:00 → Monday 06:00 date range for a given ISO week number.
    
    Args:
        week: ISO week number (1-53)
        year: ISO year. If None, uses current year.
    
    Returns:
        (start_date, end_date) - Monday 06:00 IST of that week and the following Monday
    
    Example:
        Week 36 of 2026 → 
        Start: Monday, Sep 07 2026 at 06:00 IST
        End:   Monday, Sep 14 2026 at 06:00 IST
    """
    ist = ZoneInfo("Asia/Kolkata")
    
    if year is None:
        year = datetime.now(ist).year
    
    # ISO week 1, day 1 is always a Monday
    # We can use datetime.fromisocalendar() to get Monday of a given ISO week
    monday_of_week = datetime.fromisocalendar(year, week, 1).replace(
        tzinfo=ist,
        hour=6,
        minute=0,
        second=0,
        microsecond=0
    )
    
    # End is exactly 7 days later
    monday_of_next_week = monday_of_week + timedelta(days=7)
    
    return monday_of_week, monday_of_next_week


# ════════════════════════════════════════════════════════════════════════════
# FIX #2: ELASTICSEARCH QUERY WITH PROPER RUNTIME_MAPPINGS & NESTED SUMS
# ════════════════════════════════════════════════════════════════════════════

def build_elasticsearch_aggregations(site_id: str) -> Dict[str, Any]:
    """
    Build Elasticsearch aggregations dynamically from site configuration.
    This allows different sites to have different sensor mappings.
    """
    site_config = get_site_config(site_id)
    sensor_keywords = site_config["sensor_keywords"]

    aggs = {}

    # ════════════════════════════════════════════
    # AVAILABILITY METRICS
    # ════════════════════════════════════════════
    availability_config = sensor_keywords.get("availability", {})

    if "fault_counter" in availability_config:
        fault_config = availability_config["fault_counter"]
        aggs["fault_duration"] = {
            "filter": {
                "term": {
                    "sensoridPrevEvent.keyword": fault_config["sensoridPrevEvent_keyword"]
                }
            },
            "aggs": {
                "value": {
                    "sum": {
                        "field": fault_config.get("field", "durationPrevEvent")
                    }
                }
            }
        }

    if "ideal_fault" in availability_config:
        ideal_config = availability_config["ideal_fault"]
        aggs["ideal_fault"] = {
            "filter": {
                "term": {
                    "sensoridPrevEvent.keyword": ideal_config["sensoridPrevEvent_keyword"]
                }
            },
            "aggs": {
                "value": {
                    "sum": {
                        "field": ideal_config.get("field", "durationPrevEvent")
                    }
                }
            }
        }

    aggs["total_duration"] = {
        "sum": {
            "field": "durationPrevEvent"
        }
    }

    # ════════════════════════════════════════════
    # FAULT SCALED (for performance calculation)
    # ════════════════════════════════════════════
    aggs["fault_scaled"] = {
        "filter": {
            "term": {
                "sensoridPrevEvent.keyword": availability_config.get("fault_counter", {}).get("sensoridPrevEvent_keyword", "Fault_Counter")
            }
        },
        "aggs": {
            "value": {
                "sum": {
                    "field": "scaledDurationPrevEvent"
                }
            }
        }
    }

    # ════════════════════════════════════════════
    # DOWNTIME BY PLC (WITH NESTED SUM)
    # ════════════════════════════════════════════
    downtime_config = sensor_keywords.get("downtime_by_plc", {})

    for plc_id, plc_config in downtime_config.items():
        plc_agg_name = f"{plc_id.lower()}_downtime"

        filters_dict = {}
        for fault_name, fault_config in plc_config.get("faults", {}).items():
            fault_keyword = fault_config["sensorid_keyword"]
            filter_type = fault_config.get("type", "term")

            if filter_type == "wildcard":
                filters_dict[fault_name] = {
                    "wildcard": {
                        "sensorid.keyword": fault_keyword
                    }
                }
            else:  # term
                filters_dict[fault_name] = {
                    "term": {
                        "sensorid.keyword": fault_keyword
                    }
                }

        aggs[plc_agg_name] = {
            "filter": {
                "term": {
                    "parentid.keyword": plc_config.get("parentid_filter", plc_id)
                }
            },
            "aggs": {
                "by_type": {
                    "filters": {
                        "keyed": True,
                        "filters": filters_dict
                    },
                    "aggs": {
                        "value": {
                            "sum": {
                                "field": "durationPrevEvent"
                            }
                        }
                    }
                }
            }
        }

    # ════════════════════════════════════════════
    # QUALITY METRICS
    # ════════════════════════════════════════════
    quality_config = sensor_keywords.get("quality", {})

    for quality_metric, quality_cfg in quality_config.items():
        keywords = quality_cfg.get("sensorid_keywords", [])
        filter_type = quality_cfg.get("type", "wildcard")

        should_filters = []

        for keyword in keywords:
            if filter_type == "wildcard":
                should_filters.append({
                    "wildcard": {
                        "sensorid.keyword": keyword
                    }
                })
            else:  # term
                should_filters.append({
                    "term": {
                        "sensorid.keyword": keyword
                    }
                })

        aggs[quality_metric] = {
            "filter": {
                "bool": {
                    "should": should_filters,
                    "minimum_should_match": 1
                }
            },
            "aggs": {
                "value": {
                    "sum": {
                        "field": quality_cfg.get("field", "value")
                    }
                }
            }
        }

    return aggs


def run_oee_aggregations(es: Elasticsearch, site_id: str, period_start, period_end) -> Dict:
    """
    Execute Elasticsearch aggregation query with:
    - runtime_mappings for scaledDurationPrevEvent
    - Nested sum aggregations for downtime queries
    - Site-specific PLCs and sensor mappings
    - Proper timeout and error handling
    """

    site_config = get_site_config(site_id)
    plc_ids = [plc["plc_id"] for plc in site_config["plc_config"]]

    print(f"\n[ES Query] Executing OEE aggregations for {site_id}...")
    print(f"  Period: {period_start.strftime('%Y-%m-%d %H:%M')} → {period_end.strftime('%Y-%m-%d %H:%M')}")
    print(f"  PLCs: {', '.join(plc_ids)}")
    print(f"  Index: {site_config['es_index']}")

    aggs = build_elasticsearch_aggregations(site_id)

    response = es.search(
        index=site_config["es_index"],
        size=0,
        request_timeout=300,
        query={
            "bool": {
                "filter": [
                    {
                        "terms": {
                            "parentid.keyword": plc_ids
                        }
                    },
                    {
                        "range": {
                            "@timestamp": {
                                "gte": period_start.isoformat(),
                                "lt": period_end.isoformat()
                            }
                        }
                    }
                ]
            }
        },
        # FIX #4: ADD RUNTIME_MAPPINGS FOR SCALED DURATION
        runtime_mappings={
            "scaledDurationPrevEvent": {
                "type": "double",
                "script": {
                    "source": "if (doc['durationPrevEvent'].size() != 0 && doc['valuePrevEvent'].size() != 0) { emit(doc['durationPrevEvent'].value * doc['valuePrevEvent'].value); }"
                }
            }
        },
        aggs=aggs
    )

    print("[ES Query] Complete. Processing results...")
    return response


# ════════════════════════════════════════════════════════════════════════════
# SHIFT CHANGEOVER DELAY CALCULATION
# ════════════════════════════════════════════════════════════════════════════

def calculate_shift_changeover_delays(
    es: Elasticsearch,
    site_id: str,
    start_date: datetime,
    end_date: datetime,
) -> Dict[str, float]:
    """
    Calculate cumulative shift changeover delays for all expected shift
    occurrences within the reporting period [start_date, end_date).

    The reporting period is Monday 06:00 → Monday 06:00 (exactly 168 hours).

    KEY PRINCIPLE: Shift start is defined as the first production event
    (a "good bags" sensor reading with value > 0), NOT the first
    Elasticsearch document. The sensor wildcards used to detect a
    production event come from this site's own config
    (sensor_keywords.quality.good_bags.sensorid_keywords), so this works
    unchanged across sites with different sensor naming (e.g. Shree
    Cement's 4 PLCs vs JK Cement's 2).

    SHIFT DEFINITIONS:
      - Shift A: 06:00 to 14:00 (8 hours)
      - Shift B: 14:00 to 22:00 (8 hours)
      - Shift C: 22:00 to 06:00 next day (8 hours)

    EXPECTED OCCURRENCES in a Monday 06:00 → Monday 06:00 period:
      7 x Shift A, 7 x Shift B, 7 x Shift C = 21 expected occurrences.

    If no production event occurs during a shift window, that shift does
    NOT contribute to the cumulative delay (it's simply skipped, not
    counted as a zero-delay occurrence). A shift starting production
    EARLY or exactly on time also contributes nothing — only a positive
    lag (late start) counts as delay.

    Returns:
        {"A": total_delay_minutes, "B": ..., "C": ...}
        Keys are single letters ("A"/"B"/"C") to match what
        generate_docx.py's shift-changeover table reads
        (shift_changeover.get("A"/"B"/"C")) — NOT "Shift A"/"Shift B"/
        "Shift C". That key mismatch was the reason the report always
        showed 0.00 min for every shift.
    """

    site_config = get_site_config(site_id)
    plc_ids = [plc["plc_id"] for plc in site_config["plc_config"]]

    good_bags_keywords = site_config["sensor_keywords"]["quality"]["good_bags"]["sensorid_keywords"]
    should_filters = [{"wildcard": {"sensorid.keyword": keyword}} for keyword in good_bags_keywords]

    ist = ZoneInfo("Asia/Kolkata")

    print(f"\n[Shift Query] Starting shift changeover delay calculation for {site_id}")
    print(f"  Period: {start_date.strftime('%Y-%m-%d %H:%M:%S')} → {end_date.strftime('%Y-%m-%d %H:%M:%S')}")
    query_start = time.time()

    # Build the full list of EXPECTED shift occurrences for the reporting
    # period by walking each calendar day from the reporting start,
    # creating 3 shift occurrences per day, and including only those
    # whose start falls within [start_date, end_date).
    expected_shift_occurrences = []
    current_prod_day = start_date.replace(hour=6, minute=0, second=0, microsecond=0)

    while current_prod_day < end_date:
        prod_day_start, shift_b_start, shift_c_start = calculate_shift_delimiters(current_prod_day)

        for shift_name, shift_start in [
            ("Shift A", prod_day_start),
            ("Shift B", shift_b_start),
            ("Shift C", shift_c_start),
        ]:
            shift_end = shift_start + timedelta(hours=8)

            if shift_start < end_date:
                expected_shift_occurrences.append((prod_day_start, shift_name, shift_start, shift_end))

        current_prod_day += timedelta(days=1)

    # One filtered sub-aggregation per expected occurrence, each finding
    # the earliest @timestamp where production actually began during
    # that shift window.
    shift_aggs = {}
    agg_name_lookup = {}

    for idx, (prod_day_start, shift_name, shift_start, shift_end) in enumerate(expected_shift_occurrences):
        agg_name = f"occ_{idx}"
        agg_name_lookup[agg_name] = (prod_day_start, shift_name, shift_start, shift_end)

        shift_aggs[agg_name] = {
            "filter": {
                "bool": {
                    "must": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": shift_start.isoformat(),
                                    "lt": shift_end.isoformat(),
                                }
                            }
                        },
                        {"bool": {"should": should_filters, "minimum_should_match": 1}},
                        {"range": {"value": {"gt": 0}}},
                    ]
                }
            },
            "aggs": {"first_production_ts": {"min": {"field": "@timestamp"}}},
        }

    shift_delay_response = es.search(
        index=site_config["es_index"],
        size=0,
        request_timeout=120,
        query={
            "bool": {
                "filter": [
                    {"terms": {"parentid.keyword": plc_ids}},
                    {
                        "range": {
                            "@timestamp": {
                                "gte": start_date.isoformat(),
                                "lt": end_date.isoformat(),
                            }
                        }
                    },
                ]
            }
        },
        aggs=shift_aggs,
    )

    query_elapsed = time.time() - query_start
    print(f"  Query executed in {query_elapsed:.2f}s")

    aggregations = shift_delay_response.get("aggregations", {})

    cumulative_delay_minutes = {"A": 0.0, "B": 0.0, "C": 0.0}
    shift_letter_map = {"Shift A": "A", "Shift B": "B", "Shift C": "C"}

    occurrences_with_production = 0
    occurrences_without_production = 0

    for agg_name, (prod_day_start, shift_name, shift_start, shift_end) in agg_name_lookup.items():
        agg_result = aggregations.get(agg_name, {})
        doc_count = agg_result.get("doc_count", 0)
        first_production_ts_value = agg_result.get("first_production_ts", {}).get("value")

        shift_letter = shift_letter_map[shift_name]

        if doc_count == 0 or first_production_ts_value is None:
            # No production event during this window — excluded from
            # the cumulative delay entirely, per the documented behavior.
            occurrences_without_production += 1
            continue

        occurrences_with_production += 1

        # first_production_ts_value is epoch milliseconds (ES "min" agg
        # on a date field). Convert to an aware IST datetime so it can
        # be compared directly against shift_start.
        first_production_dt = datetime.fromtimestamp(first_production_ts_value / 1000, tz=ist)
        shift_start_ist = (
            shift_start.astimezone(ist) if shift_start.tzinfo else shift_start.replace(tzinfo=ist)
        )

        lag_minutes = (first_production_dt - shift_start_ist).total_seconds() / 60.0

        # Only a POSITIVE lag (production genuinely started late) counts
        # as a changeover delay; starting early/on time never subtracts.
        if lag_minutes > 0:
            cumulative_delay_minutes[shift_letter] += lag_minutes

    total_elapsed = time.time() - query_start
    print(f"  Shift changeover delay calculation complete in {total_elapsed:.2f}s")
    print(f"    Occurrences with production data : {occurrences_with_production}")
    print(f"    Occurrences with NO production    : {occurrences_without_production}")
    print(f"    Shift A cumulative delay : {cumulative_delay_minutes['A']:.2f} min")
    print(f"    Shift B cumulative delay : {cumulative_delay_minutes['B']:.2f} min")
    print(f"    Shift C cumulative delay : {cumulative_delay_minutes['C']:.2f} min")

    return cumulative_delay_minutes


# ════════════════════════════════════════════════════════════════════════════
# PER-SPOUT DOWNTIME BY PLC
# ════════════════════════════════════════════════════════════════════════════

DOWNTIME_DIVISOR = 608.33  # same conversion factor used elsewhere in this file


def _wildcard_to_regex(wildcard_pattern: str) -> str:
    """
    Convert a Lucene-style wildcard pattern — the kind already used in
    SITE_CONFIG's sensorid_keyword filters, e.g. "FAULT_OPEN_SP*" or
    "PACKER02_FAULT_OPEN_SP*" — into an equivalent regex, because a terms
    aggregation's "include" parameter requires a regex, not a Lucene
    wildcard. Only '*' (any sequence) and '?' (any single char) are
    treated as wildcards; everything else is regex-escaped.
    """
    converted = []
    for char in wildcard_pattern:
        if char == "*":
            converted.append(".*")
        elif char == "?":
            converted.append(".")
        else:
            converted.append(re.escape(char))
    return "".join(converted)


def get_plc_spout_downtime(
    es: Elasticsearch,
    site_id: str,
    period_start: datetime,
    period_end: datetime,
) -> Dict[str, Dict[int, float]]:
    """
    Per-spout downtime minutes, per PLC, using the same terms-aggregation
    approach as the Vega spec:

        {"terms": {"field": "sensorid.keyword", "include": "<pattern>", "size": 16}}

    scoped per PLC via an outer filter aggregation on parentid.keyword.

    <pattern> is NOT hardcoded — it's derived, per PLC, from this site's
    own SITE_CONFIG (sensor_keywords.downtime_by_plc[plc_id].faults
    ["Spout Fault"].sensorid_keyword) — the exact same wildcard already
    used elsewhere in this file to compute that PLC's aggregate "Spout
    Fault" downtime total — converted from Lucene wildcard syntax to the
    regex syntax "include" requires. That matters because the wildcard
    is NOT uniform across PLCs: JK Cement's PLC_01 uses
    "FAULT_OPEN_SP*" while its PLC_02 uses "PACKER02_FAULT_OPEN_SP*" (a
    completely different prefix, not a "P02_SP" suffix pattern), and
    Shree Cement has 4 PLCs. Deriving the pattern from config instead of
    guessing it means this works unchanged for every configured PLC on
    every site.

    Downtime Minutes = doc_count / 608.33, rounded to 2 decimal places.

    Sensor name -> spout number: only the digits immediately following
    "SP" at the END of the sensor key are read as the spout number (e.g.
    "PACKER02_FAULT_OPEN_SP01" -> spout 1). This avoids any other digit
    group earlier in the key (a PLC number, a packer number, etc.)
    leaking into the spout number.

    Returns:
        {"PLC_01": {1: 23.41, 2: 18.27, ...}, "PLC_02": {...}, ...}
        One entry per PLC configured for this site (dict keys are spout
        numbers as ints, sorted ascending). A PLC with no data, or no
        "Spout Fault" sensor config at all, gets {}.
    """
    site_config = get_site_config(site_id)
    plc_ids = [plc["plc_id"] for plc in site_config["plc_config"]]
    downtime_config = site_config["sensor_keywords"].get("downtime_by_plc", {})

    print(f"\n[Spout Downtime Query] Starting per-PLC spout downtime aggregation for {site_id}")
    print(f"  Period: {period_start.strftime('%Y-%m-%d %H:%M:%S')} → {period_end.strftime('%Y-%m-%d %H:%M:%S')}")
    query_start = time.time()

    aggs = {}
    plc_agg_names = {}

    for plc_id in plc_ids:
        plc_config = downtime_config.get(plc_id, {})
        spout_fault_config = plc_config.get("faults", {}).get("Spout Fault")

        if not spout_fault_config:
            print(f"  ⚠ No 'Spout Fault' sensor config for {plc_id}; skipping")
            continue

        include_pattern = _wildcard_to_regex(spout_fault_config["sensorid_keyword"])
        agg_name = f"{plc_id.lower()}_spout"
        plc_agg_names[plc_id] = agg_name

        aggs[agg_name] = {
            "filter": {"term": {"parentid.keyword": plc_config.get("parentid_filter", plc_id)}},
            "aggs": {
                "spout_downtime": {
                    "terms": {
                        "field": "sensorid.keyword",
                        "include": include_pattern,
                        "size": 16,
                    }
                }
            },
        }

    result = {plc_id: {} for plc_id in plc_ids}

    if not aggs:
        print("  ⚠ No PLCs with spout-fault sensor config; returning empty result for all PLCs")
        return result

    response = es.search(
        index=site_config["es_index"],
        size=0,
        request_timeout=120,
        query={
            "bool": {
                "filter": [
                    {"terms": {"parentid.keyword": plc_ids}},
                    {"range": {"@timestamp": {"gte": period_start.isoformat(), "lt": period_end.isoformat()}}},
                ]
            }
        },
        aggs=aggs,
    )

    query_elapsed = time.time() - query_start
    print(f"[Spout Downtime Query] Completed in {query_elapsed:.2f} seconds")

    def parse_spout_buckets(buckets):
        parsed = {}
        for bucket in buckets:
            sensor_key = bucket["key"]
            doc_count = bucket["doc_count"]

            # Match "SP" followed by digits, anchored to the END of the
            # key — so an unrelated digit group earlier in the key (a
            # PLC/packer number, e.g. the "02" in "PACKER02_") can never
            # leak into the spout number.
            match = re.search(r"SP(\d+)$", sensor_key, re.IGNORECASE)
            if not match:
                continue

            spout_num = int(match.group(1))
            parsed[spout_num] = round(doc_count / DOWNTIME_DIVISOR, 2)

        return dict(sorted(parsed.items()))

    aggregations = response.get("aggregations", {})

    print()
    print("========================================")
    print("PLC-WISE SPOUT DOWNTIME")
    print("========================================")

    for plc_id in plc_ids:
        agg_name = plc_agg_names.get(plc_id)

        if agg_name is not None:
            buckets = aggregations.get(agg_name, {}).get("spout_downtime", {}).get("buckets", [])
            result[plc_id] = parse_spout_buckets(buckets)

        print(plc_id)
        if result[plc_id]:
            for spout_num, minutes in result[plc_id].items():
                print(f"  Spout {spout_num} : {minutes:.2f} min")
        else:
            print("  No spout downtime recorded")

    print("========================================")

    return result


# ════════════════════════════════════════════════════════════════════════════
# FIX #3: CORRECT AVAILABILITY FORMULA & OEE CALCULATION
# ════════════════════════════════════════════════════════════════════════════

def calculate_oee_metrics(response: Dict, site_id: str) -> Dict:
    """
    Calculate OEE using CORRECT FORMULAS.

    KEY FIX: Availability uses downtime_divisor of 608.33
    This represents the expected production minutes in a 7-day week.

    Availability = (1 - (total_fault_time / 608.33)) * 100

    NOTE: This function returns the FLAT internal "metrics" dict used only
    for calculation. It is intentionally NOT the same shape as analysis.json
    — save_analysis_json() below builds the nested, documented schema from
    this dict. Callers that want the analysis.json-shaped object should use
    what run_analysis_pipeline() returns, not this function's return value.
    """

    aggs = response.get("aggregations", {})

    # Extract raw values
    fault_duration = aggs["fault_duration"]["value"]["value"]
    fault_scaled = aggs["fault_scaled"]["value"]["value"]
    ideal_fault = aggs["ideal_fault"]["value"]["value"]
    total_duration = aggs["total_duration"]["value"]
    good_bags = int(aggs["good_bags"]["value"]["value"])
    burst_bags = int(aggs["burst_bags"]["value"]["value"])
    out_of_limit_bags = int(aggs["out_of_limit_bags"]["value"]["value"])

    print(f"\n[{site_id}] Raw aggregation values:")
    print(f"  Fault duration: {fault_duration:.2f}")
    print(f"  Fault scaled: {fault_scaled:.2f}")
    print(f"  Ideal fault: {ideal_fault:.2f}")
    print(f"  Good bags: {good_bags}")
    print(f"  Burst bags: {burst_bags}")
    print(f"  Out of limit: {out_of_limit_bags}")

    # ════════════════════════════════════════════════════════════════════════
    # AVAILABILITY CALCULATION (FROM CORRECT REFERENCE CODE)
    # Vega Formula: ((fault_duration*16) - fault_scaled + (ideal_fault*16)) / (total_duration*16)
    # ════════════════════════════════════════════════════════════════════════
    availability_numerator = (fault_duration * 16.0) - fault_scaled + (ideal_fault * 16.0)
    availability_denominator = total_duration * 16.0

    availability = (
        availability_numerator / availability_denominator
        if availability_denominator != 0
        else 0
    )
    availability = max(0, min(1, availability))  # Clamp to [0, 1]
    availability_pct = availability * 100

    # ════════════════════════════════════════════════════════════════════════
    # RUNNING SECONDS (FROM CORRECT REFERENCE CODE)
    # Vega Formula: runningSeconds = (fault_duration*16) - fault_scaled
    # ════════════════════════════════════════════════════════════════════════
    running_seconds = (fault_duration * 16.0) - fault_scaled

    # ════════════════════════════════════════════════════════════════════════
    # PERFORMANCE (FROM CORRECT REFERENCE CODE)
    # Vega Formula: good_bags / (runningSeconds/60 * 5)
    # The "5" = standard production rate of 5 bags per minute
    # ════════════════════════════════════════════════════════════════════════
    expected_good_bags = (running_seconds / 60.0) * 5.0 if running_seconds > 0 else 0

    performance = (
        good_bags / expected_good_bags
        if expected_good_bags > 0
        else 0
    )
    performance = max(0, min(1, performance))  # Clamp to [0, 1]
    performance_pct = performance * 100

    # ════════════════════════════════════════════════════════════════════════
    # QUALITY (FROM CORRECT REFERENCE CODE)
    # Vega Formula: good_bags / total_bags
    # ════════════════════════════════════════════════════════════════════════
    total_bags = good_bags + burst_bags + out_of_limit_bags
    quality = (
        good_bags / total_bags
        if total_bags != 0
        else 0
    )
    quality = max(0, min(1, quality))  # Clamp to [0, 1]
    quality_pct = quality * 100

    # ════════════════════════════════════════════════════════════════════════
    # OEE (FROM CORRECT REFERENCE CODE)
    # Vega Formula: Availability * Performance * Quality
    # ════════════════════════════════════════════════════════════════════════
    oee_decimal = availability * performance * quality
    oee_decimal = max(0, min(1, oee_decimal))  # Clamp to [0, 1]
    oee_pct = oee_decimal * 100.0

    print(f"\n[{site_id}] Calculated metrics:")
    print(f"  Availability: {availability_pct:.2f}%")
    print(f"  Performance: {performance_pct:.2f}%")
    print(f"  Quality: {quality_pct:.2f}%")
    print(f"  OEE: {oee_pct:.2f}%")

    return {
        "availability": availability,
        "availability_pct": availability_pct,
        "availability_numerator": availability_numerator,
        "availability_denominator": availability_denominator,
        "performance": performance,
        "performance_pct": performance_pct,
        "quality": quality,
        "quality_pct": quality_pct,
        "oee_decimal": oee_decimal,
        "oee_pct": oee_pct,
        "good_bags": good_bags,
        "burst_bags": burst_bags,
        "out_of_limit_bags": out_of_limit_bags,
        "total_bags": total_bags,
        "running_seconds": running_seconds,
        "expected_good_bags": expected_good_bags,
        "total_duration": total_duration,
        "fault_duration": fault_duration,
        "fault_scaled": fault_scaled,
        "ideal_fault": ideal_fault,
        "aggregations": aggs,
    }


# ════════════════════════════════════════════════════════════════════════════
# FIX #3: PROPER EXCEL GENERATION WITH CORRECT OUTPUT PATH
# ════════════════════════════════════════════════════════════════════════════

def generate_excel_report(metrics: Dict, site_id: str, week: int, output_dir: Path) -> Path:
    """
    Generate a comprehensive Excel report containing the same analysis data
    exposed in analysis.json, including:
    - Report metadata and period
    - Verified KPIs
    - KPI details
    - Availability details
    - Bag/quality metrics
    - Performance details
    - Downtime by PLC
    - Shift delimiters for the reporting week
    - Raw aggregation data
    """
    report_dir = output_dir / str(week)
    report_dir.mkdir(parents=True, exist_ok=True)

    excel_file = report_dir / "report.xlsx"

    print(f"\n[{site_id}] Generating comprehensive Excel report...")
    print(f"  Path: {excel_file}")

    site_config = get_site_config(site_id)

    shift_changeover_minutes = metrics.get("shift_changeover_minutes", {})

    # ------------------------------------------------------------------------
    # Shift delimiters: Monday 06:00 -> next Monday 06:00
    # ------------------------------------------------------------------------
    period_start = metrics.get("period_start")
    period_end = metrics.get("period_end")

    if period_start is not None and period_end is not None:
        shift_rows = []
        current = period_start

        while current < period_end:
            prod_day_start, shift_b_start, shift_c_start = calculate_shift_delimiters(current)

            # Only include complete shift boundaries within this report period.
            shift_rows.extend([
                {
                    "Shift": "Shift A",
                    "Production Day": prod_day_start.strftime("%Y-%m-%d"),
                    "Start": prod_day_start.strftime("%Y-%m-%d %H:%M:%S IST"),
                    "End": shift_b_start.strftime("%Y-%m-%d %H:%M:%S IST"),
                },
                {
                    "Shift": "Shift B",
                    "Production Day": prod_day_start.strftime("%Y-%m-%d"),
                    "Start": shift_b_start.strftime("%Y-%m-%d %H:%M:%S IST"),
                    "End": shift_c_start.strftime("%Y-%m-%d %H:%M:%S IST"),
                },
                {
                    "Shift": "Shift C",
                    "Production Day": prod_day_start.strftime("%Y-%m-%d"),
                    "Start": shift_c_start.strftime("%Y-%m-%d %H:%M:%S IST"),
                    "End": (prod_day_start + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S IST"),
                },
            ])

            current = prod_day_start + timedelta(days=1)

        # Keep only shifts whose start is inside the reporting period.
        shift_rows = [
            r for r in shift_rows
            if period_start.strftime("%Y-%m-%d %H:%M:%S") <=
               r["Start"].replace(" IST", "") <
               period_end.strftime("%Y-%m-%d %H:%M:%S")
        ]
    else:
        shift_rows = []

    # ------------------------------------------------------------------------
    # Main summary sheet
    # ------------------------------------------------------------------------
    rows = [
        {"Parameter": "Report Period", "Value": ""},
        {"Parameter": "Start Date", "Value": metrics.get("period_start_str", "")},
        {"Parameter": "End Date", "Value": metrics.get("period_end_str", "")},
        {"Parameter": "Week", "Value": week},
        {"Parameter": "Previous Week", "Value": week - 1},
        {"Parameter": "Equipment", "Value": ", ".join(
            plc["name"] for plc in site_config["plc_config"]
        )},
        {"Parameter": "", "Value": ""},

        {"Parameter": "Business-Relevant KPIs", "Value": ""},
        {"Parameter": "OEE", "Value": f"{metrics['oee_pct']:.2f}%"},
        {"Parameter": "Availability", "Value": f"{metrics['availability_pct']:.2f}%"},
        {"Parameter": "Performance", "Value": f"{metrics['performance_pct']:.2f}%"},
        {"Parameter": "Quality", "Value": f"{metrics['quality_pct']:.2f}%"},
        {"Parameter": "", "Value": ""},

        {"Parameter": "Availability Details", "Value": ""},
        {"Parameter": "Planned Production Time (min)", "Value": (
            metrics.get("total_duration", 0) / 60.0 -
            metrics.get("fault_duration", 0) / 60.0
        )},
        {"Parameter": "Total Available Time (min)", "Value": (
            metrics.get("total_duration", 0) / 60.0
        )},
        {"Parameter": "", "Value": ""},

        {"Parameter": "Bag Metrics", "Value": ""},
        {"Parameter": "Good Bags", "Value": metrics["good_bags"]},
        {"Parameter": "Burst Bags", "Value": metrics["burst_bags"]},
        {"Parameter": "Out of Limit Bags", "Value": metrics["out_of_limit_bags"]},
        {"Parameter": "Total Bags", "Value": metrics["total_bags"]},
        {"Parameter": "", "Value": ""},

        {"Parameter": "Performance Details", "Value": ""},
        {"Parameter": "Actual Good Bags", "Value": metrics["good_bags"]},
        {"Parameter": "Expected Good Bags", "Value": round(metrics["expected_good_bags"], 2)},
        {"Parameter": "Running Seconds", "Value": round(metrics["running_seconds"], 2)},
        {"Parameter": "", "Value": ""},

        {"Parameter": "Downtime Breakdown By PLC", "Value": ""},
    ]

    # ------------------------------------------------------------------------
    # PLC downtime
    # ------------------------------------------------------------------------
    for plc_config in site_config["plc_config"]:
        plc_id = plc_config["plc_id"]
        agg_key = f"{plc_id.lower()}_downtime"

        if agg_key in metrics.get("aggregations", {}):
            buckets = metrics["aggregations"][agg_key]["by_type"]["buckets"]

            for fault_type, fault_data in buckets.items():
                if "value" in fault_data and "value" in fault_data["value"]:
                    seconds = fault_data["value"]["value"]
                    minutes = seconds / 60.0

                    rows.append({
                        "Parameter": f"{plc_id} {fault_type}",
                        "Value": round(minutes, 2)
                    })

    rows.extend([
        {"Parameter": "", "Value": ""},
        {"Parameter": "Raw Aggregation Data", "Value": ""},
        {"Parameter": "Good Bags", "Value": metrics["good_bags"]},
        {"Parameter": "Burst Bags", "Value": metrics["burst_bags"]},
        {"Parameter": "Out of Limit Bags", "Value": metrics["out_of_limit_bags"]},
        {"Parameter": "Total Bags", "Value": metrics["total_bags"]},
        {"Parameter": "Fault Duration (sec)", "Value": round(metrics["fault_duration"], 2)},
        {"Parameter": "Fault Scaled (sec)", "Value": round(metrics["fault_scaled"], 2)},
        {"Parameter": "Ideal Fault (sec)", "Value": round(metrics["ideal_fault"], 2)},
        {"Parameter": "Total Duration (sec)", "Value": round(metrics["total_duration"], 2)},
        {"Parameter": "Running Seconds", "Value": round(metrics["running_seconds"], 2)},
        {"Parameter": "", "Value": ""},
        {"Parameter": "Shift Changeover Delays (minutes)", "Value": ""},
        {"Parameter": "Shift A Changeover Delay", "Value": f"{shift_changeover_minutes.get('A', 0.0):.2f} min"},
        {"Parameter": "Shift B Changeover Delay", "Value": f"{shift_changeover_minutes.get('B', 0.0):.2f} min"},
        {"Parameter": "Shift C Changeover Delay", "Value": f"{shift_changeover_minutes.get('C', 0.0):.2f} min"},
    ])

    summary_df = pd.DataFrame(rows)

    # ------------------------------------------------------------------------
    # Shift schedule sheet
    # ------------------------------------------------------------------------
    shift_df = pd.DataFrame(
        shift_rows,
        columns=["Shift", "Production Day", "Start", "End"]
    )

    # ------------------------------------------------------------------------
    # KPI details sheet
    # ------------------------------------------------------------------------
    kpi_df = pd.DataFrame([
        ["OEE", round(metrics["oee_pct"], 2), f"{metrics['oee_pct']:.2f}%"],
        ["Availability", round(metrics["availability_pct"], 2), f"{metrics['availability_pct']:.2f}%"],
        ["Performance", round(metrics["performance_pct"], 2), f"{metrics['performance_pct']:.2f}%"],
        ["Quality", round(metrics["quality_pct"], 2), f"{metrics['quality_pct']:.2f}%"],
    ], columns=["KPI", "Raw Value", "Display Value"])

    # ------------------------------------------------------------------------
    # Quality details sheet
    # ------------------------------------------------------------------------
    quality_df = pd.DataFrame([
        ["Good Bags", metrics["good_bags"], str(metrics["good_bags"])],
        ["Burst Bags", metrics["burst_bags"], str(metrics["burst_bags"])],
        ["Out of Limit Bags", metrics["out_of_limit_bags"], str(metrics["out_of_limit_bags"])],
        ["Total Bags", metrics["total_bags"], str(metrics["total_bags"])],
    ], columns=["Metric", "Raw Value", "Display Value"])

    # ------------------------------------------------------------------------
    # Write workbook
    # ------------------------------------------------------------------------
    with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        kpi_df.to_excel(writer, sheet_name="KPI Details", index=False)
        quality_df.to_excel(writer, sheet_name="Quality Details", index=False)
        shift_df.to_excel(writer, sheet_name="Shift Schedule", index=False)

    # Basic formatting
    from openpyxl import load_workbook
    wb = load_workbook(excel_file)

    for ws in wb.worksheets:
        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 42
        ws.column_dimensions["B"].width = 28
        ws.column_dimensions["C"].width = 28
        ws.column_dimensions["D"].width = 28

        # Bold section/header rows in Summary
        if ws.title == "Summary":
            for row in ws.iter_rows():
                if row[0].value in {
                    "Report Period",
                    "Business-Relevant KPIs",
                    "Availability Details",
                    "Bag Metrics",
                    "Performance Details",
                    "Downtime Breakdown By PLC",
                    "Raw Aggregation Data",
                    "Shift Changeover Delays (minutes)",
                }:
                    for cell in row:
                        cell.font = cell.font.copy(bold=True)

    wb.save(excel_file)

    print(f"  ✓ Comprehensive Excel saved: {excel_file}")
    print(f"  ✓ Includes Summary, KPI Details, Quality Details and Shift Schedule")

    return excel_file


# ════════════════════════════════════════════════════════════════════════════
# FIX #3: ANALYSIS JSON CREATION (CORRECT PATH)
# ════════════════════════════════════════════════════════════════════════════

def load_previous_week_kpi(week: int, output_dir: Path) -> Dict[str, float]:
    """
    Load the previous week's kpi_percent from its already-saved
    analysis.json (output/<site>/<week-1>/analysis.json), so the
    Week-over-Week comparison table has something to compare against.

    Returns {} if the previous week's report hasn't been generated yet
    (e.g. the first week this pipeline runs for a site, or a gap week) —
    the comparison table then correctly falls back to "-" / "No prior
    data" rather than inventing a number.
    """
    previous_analysis_file = output_dir / str(week - 1) / "analysis.json"

    if not previous_analysis_file.exists():
        print(f"  (no previous-week analysis at {previous_analysis_file} — "
              f"week-over-week comparison will show 'No prior data')")
        return {}

    try:
        with open(previous_analysis_file, "r", encoding="utf-8") as file:
            previous_analysis = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        print(f"  ⚠ Could not read previous week's analysis.json: {error}")
        return {}

    previous_kpi = previous_analysis.get("kpi_percent", {})

    if previous_kpi:
        print(f"  ✓ Loaded previous week's KPIs from {previous_analysis_file}")

    return previous_kpi


def save_analysis_json(metrics: Dict, site_id: str, week: int, output_dir: Path) -> Tuple[Path, Dict]:
    """
    Build and save complete analysis.json with all available metrics and
    details.

    This includes:
    - KPI percentages (oee, availability, performance, quality)
    - Quality details (good/burst/out of limit bags)
    - Performance details (actual vs expected bags)
    - Availability details (time values)
    - Downtime breakdown by PLC and fault type
    - Raw aggregation data
    - Shift changeover (if available)

    Output path: output/<site>/<week>/analysis.json

    Returns:
        (analysis_file, analysis) — the saved file's path AND the exact
        dict that was written to it. Callers (run_analysis_pipeline) must
        propagate the "analysis" dict, not the flat "metrics" dict passed
        in, so that in-memory consumers (Stage 1.5 / generate_insights)
        see the same shape as what's on disk.
    """
 
    report_dir = output_dir / str(week)
    report_dir.mkdir(parents=True, exist_ok=True)
 
    analysis_file = report_dir / "analysis.json"
 
    print(f"[{site_id}] Saving analysis JSON...")
    print(f"  Path: {analysis_file}")
 
    # ════════════════════════════════════════════════════════════════════════
    # BUILD AVAILABILITY DETAILS (with _min suffix for minutes)
    # ════════════════════════════════════════════════════════════════════════
    
    # Calculate planned production time and total available time from metrics
    # These come from the ES aggregation totals
    total_duration_sec = metrics.get("total_duration", 0)  # From ES: total durationPrevEvent
    fault_duration_sec = metrics.get("fault_duration", 0)  # From ES: Fault_Counter duration
    
    total_duration_min = total_duration_sec / 60.0
    fault_duration_min = fault_duration_sec / 60.0
    planned_production_time_min = total_duration_min - fault_duration_min
    
    availability_details = {
        "planned_production_time_min": planned_production_time_min,
        "total_available_time_min": total_duration_min,
    }
 
    # ════════════════════════════════════════════════════════════════════════
    # BUILD DOWNTIME BREAKDOWN BY PLC
    # ════════════════════════════════════════════════════════════════════════
    
    downtime_breakdown_by_plc = {}
    site_config = get_site_config(site_id)
    
    for plc_config in site_config["plc_config"]:
        plc_id = plc_config["plc_id"]
        agg_key = f"{plc_id.lower()}_downtime"
        
        # Initialize PLC entry with machine name
        plc_entry = {
            "machine": plc_config.get("name", plc_id)
        }
        
        # Extract fault breakdowns from aggregations
        if agg_key in metrics.get("aggregations", {}):
            by_type_buckets = metrics["aggregations"][agg_key]["by_type"]["buckets"]
            
            for fault_name, fault_data in by_type_buckets.items():
                if "value" in fault_data and "value" in fault_data["value"]:
                    fault_duration_seconds = fault_data["value"]["value"]
                    fault_duration_minutes = fault_duration_seconds / 60.0
                    
                    # Use _min suffix for consistency with old code
                    key_name = fault_name.lower().replace(" ", "_") + "_min"
                    plc_entry[key_name] = fault_duration_minutes
        
        downtime_breakdown_by_plc[plc_id] = plc_entry

    # ════════════════════════════════════════════════════════════════════════
    # BUILD SHIFT CHANGEOVER (from calculate_shift_changeover_delays)
    # ════════════════════════════════════════════════════════════════════════

    shift_changeover_minutes = metrics.get("shift_changeover_minutes", {})

    # Keys are single letters ("A"/"B"/"C") — this is what
    # generate_docx.py's shift-changeover table reads via
    # shift_changeover.get("A"/"B"/"C"). Using "Shift A"/etc. here was
    # the reason the report used to show 0.00 min for every shift.
    shift_changeover = {
        letter: {
            "raw_value": round(shift_changeover_minutes.get(letter, 0.0), 2),
            "display_value": f"{shift_changeover_minutes.get(letter, 0.0):.2f} min",
            "json_value": round(shift_changeover_minutes.get(letter, 0.0), 2),
        }
        for letter in ("A", "B", "C")
    }

    # ════════════════════════════════════════════════════════════════════════
    # BUILD COMPLETE ANALYSIS JSON
    # ════════════════════════════════════════════════════════════════════════
 
    analysis = {
        # ════ METADATA ════
        "source": {
            "file": "Elasticsearch aggregations",
            "mode": "ES_QUERY",
            "calculation_performed": True,
            "source_of_truth": "Elasticsearch IoT data"
        },
        "machine": ", ".join([plc["name"] for plc in site_config["plc_config"]]),
        
        # ════ VERIFIED KPIs ════
        "verified_kpis": {
            "oee": round(metrics["oee_pct"], 2),
            "availability": round(metrics["availability_pct"], 2),
            "performance": round(metrics["performance_pct"], 2),
            "quality": round(metrics["quality_pct"], 2),
        },
        
        # ════ WEEK INFO ════
        "week": week,
        "previous_week": week - 1,
        
        # ════ REPORT PERIOD ════
        "report_period": {
            "start": metrics.get("period_start_str", ""),
            "end": metrics.get("period_end_str", ""),
            "start_raw": metrics.get("period_start_str", ""),
            "end_raw": metrics.get("period_end_str", ""),
        },
        "previous_report_period": {
            "start": "",  # Would be calculated from week-1 if needed
            "end": "",
        },
        
        # ════ PREVIOUS KPI (if available) ════
        "previous_kpi": metrics.get("previous_kpi", {}),
        "previous_kpi_available": bool(metrics.get("previous_kpi", {})),
        
        # ════ KPI (cell_info style - for compatibility) ════
        "kpi": {
            "oee": {
                "raw_value": round(metrics["oee_pct"], 2),
                "display_value": f"{metrics['oee_pct']:.2f}%",
                "json_value": f"{metrics['oee_pct']:.2f}%"
            },
            "availability": {
                "raw_value": round(metrics["availability_pct"], 2),
                "display_value": f"{metrics['availability_pct']:.2f}%",
                "json_value": f"{metrics['availability_pct']:.2f}%"
            },
            "performance": {
                "raw_value": round(metrics["performance_pct"], 2),
                "display_value": f"{metrics['performance_pct']:.2f}%",
                "json_value": f"{metrics['performance_pct']:.2f}%"
            },
            "quality": {
                "raw_value": round(metrics["quality_pct"], 2),
                "display_value": f"{metrics['quality_pct']:.2f}%",
                "json_value": f"{metrics['quality_pct']:.2f}%"
            },
        },
        
        # ════ KPI PERCENT (flat) ════
        "kpi_percent": {
            "oee": round(metrics["oee_pct"], 2),
            "availability": round(metrics["availability_pct"], 2),
            "performance": round(metrics["performance_pct"], 2),
            "quality": round(metrics["quality_pct"], 2),
        },
        
        # ════ AVAILABILITY DETAILS ════
        "availability_details": availability_details,
        
        # ════ DOWNTIME BREAKDOWN BY PLC ════
        "downtime_breakdown_by_plc": downtime_breakdown_by_plc,

        # ════ SHIFT CHANGEOVER ════
        "shift_changeover": shift_changeover,

        # ════ SPOUT DOWNTIME BY PLC ════
        # {plc_id: {spout_num: minutes}} — JSON serializes int keys as
        # strings, generate_charts.py's spout chart reads them back as
        # such. metrics.get(...) covers a call site that doesn't pass
        # spout data through (defensive; run_analysis_pipeline always does).
        "spout_downtime": metrics.get("spout_downtime", {}),
        
        # ════ QUALITY DETAILS ════
        "quality_details": {
            "good_bags": {
                "raw_value": metrics["good_bags"],
                "display_value": str(metrics["good_bags"]),
                "json_value": metrics["good_bags"]
            },
            "burst_bags": {
                "raw_value": metrics["burst_bags"],
                "display_value": str(metrics["burst_bags"]),
                "json_value": metrics["burst_bags"]
            },
            "out_of_limit_bags": {
                "raw_value": metrics["out_of_limit_bags"],
                "display_value": str(metrics["out_of_limit_bags"]),
                "json_value": metrics["out_of_limit_bags"]
            },
            "total_bags": {
                "raw_value": metrics["total_bags"],
                "display_value": str(metrics["total_bags"]),
                "json_value": metrics["total_bags"]
            },
        },
        
        # ════ PERFORMANCE DETAILS ════
        "performance_details": {
            "actual_good_bags": {
                "raw_value": metrics["good_bags"],
                "display_value": f"{metrics['good_bags']:.2f}",
                "json_value": metrics["good_bags"]
            },
            "expected_good_bags": {
                "raw_value": round(metrics["expected_good_bags"], 2),
                "display_value": f"{metrics['expected_good_bags']:.2f}",
                "json_value": round(metrics["expected_good_bags"], 2)
            },
        },
        
        # ════ RAW AGGREGATION DATA ════
        "raw_aggregation_data": {
            "good_bags": metrics["good_bags"],
            "burst_bags": metrics["burst_bags"],
            "out_of_limit": metrics["out_of_limit_bags"],
            "total_bags": metrics["total_bags"],
            "fault_duration": round(metrics["fault_duration"], 2),
            "fault_scaled": round(metrics["fault_scaled"], 2),
            "ideal_fault": round(metrics["ideal_fault"], 2),
            "total_duration": round(metrics["total_duration"], 2),
            "running_seconds": round(metrics["running_seconds"], 2),
        },
    }
 
    # ════════════════════════════════════════════════════════════════════════
    # SAVE TO FILE
    # ════════════════════════════════════════════════════════════════════════
 
    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=4)
 
    print(f"  ✓ Analysis JSON saved: {analysis_file}")
 
    return analysis_file, analysis
 

# ════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def run_analysis_pipeline(site_id: str, week: int, config_manager) -> Tuple[Dict, Dict]:
    """
    Complete OEE analysis pipeline with all fixes applied:
    1. Proper Monday 06:00 → Monday 06:00 date ranges
    2. Correct availability formula (608.33 divisor)
    3. Consistent output paths
    4. Elasticsearch runtime_mappings
    5. Nested sum aggregations
    6. Multi-site support with configurable mappings
    
    Args:
        site_id: Site identifier (e.g., "jk_cement_aligarh")
        week: ISO week number (e.g., 36)
        config_manager: ConfigManager instance with ES and site configuration
    
    Returns:
        (analysis_dict, paths_dict) — the exact same nested dict that was
        written to analysis.json (with verified_kpis / kpi_percent /
        quality_details / downtime_breakdown_by_plc / machine / week etc.),
        NOT the flat internal "metrics" dict from calculate_oee_metrics().
        This is what gets passed downstream to Stage 1.5's
        generate_insights(), so it must match what's on disk.
    """

    # Get paths for this site/week
    paths_config = config_manager.get_paths(site_id, week)
    output_dir = paths_config["week_output_dir"].parent  # Parent of week dir = output_folder

    # Get basic site config from config_manager (for paths)
    basic_site_config = config_manager.get_site_config(site_id)
    
    # Get full site config from hardcoded SITE_CONFIG (for sensor keywords, PLC config, etc.)
    site_config = get_site_config(site_id)

    print(f"\n{'='*70}")
    print(f"OEE ANALYSIS PIPELINE - {site_config['name']}")
    print(f"{'='*70}\n")

    print(f"Week: {week}")

    # Get Elasticsearch configuration from config_manager
    # NOTE: get_es_config() returns {"url": ..., "api_key": ...}
    es_config = config_manager.get_es_config()
    
    # Connect to Elasticsearch
    es = Elasticsearch(
        [es_config["url"]],
        api_key=es_config["api_key"]
    )

    try:
        # Calculate date range for this week
        period_start, period_end = get_monday_6am_for_week(week)
        
        print(f"Period: {period_start.strftime('%Y-%m-%d %H:%M IST')} → {period_end.strftime('%Y-%m-%d %H:%M IST')}")
        
        # Run aggregations with fixes
        response = run_oee_aggregations(es, site_id, period_start, period_end)

        # Calculate metrics with CORRECT formulas (flat internal shape —
        # NOT what gets returned to the caller; see save_analysis_json below)
        metrics = calculate_oee_metrics(response, site_id)

        # Add dates to metrics for Excel/JSON
        metrics["period_start"] = period_start
        metrics["period_end"] = period_end
        metrics["period_start_str"] = period_start.strftime("%Y-%m-%d %H:%M:%S IST")
        metrics["period_end_str"] = period_end.strftime("%Y-%m-%d %H:%M:%S IST")

        # Shift changeover delays — a separate ES query, since it needs a
        # per-occurrence (21x) sub-aggregation rather than the single
        # summary aggregation the rest of the KPIs use.
        metrics["shift_changeover_minutes"] = calculate_shift_changeover_delays(
            es, site_id, period_start, period_end
        )

        # Previous week's KPIs, for the Week-over-Week comparison table.
        # This was never populated before — the comparison table always
        # showed "-" / "No prior data" regardless of whether a previous
        # week had actually been run.
        metrics["previous_kpi"] = load_previous_week_kpi(week, output_dir)

        # Per-spout downtime by PLC, for the "2.2 Spout Downtime by
        # Machine" report section / charts.
        metrics["spout_downtime"] = get_plc_spout_downtime(es, site_id, period_start, period_end)

        # Generate Excel with correct path
        excel_file = generate_excel_report(metrics, site_id, week, output_dir)

        # Save analysis JSON with correct path.
        # FIX: capture the nested "analysis" dict this returns (in addition
        # to the file path) so we can hand it back to the caller instead of
        # the flat "metrics" dict.
        analysis_file, analysis = save_analysis_json(metrics, site_id, week, output_dir)

        print(f"\n{'='*70}")
        print("✓ Pipeline Complete")
        print(f"{'='*70}\n")

        paths = {
            "excel_file": str(excel_file),
            "analysis_json": str(analysis_file),
            "output_dir": str(output_dir),
        }

        # FIX: return the nested "analysis" dict (matches analysis.json on
        # disk), not the flat "metrics" dict. This is what Stage 1.5 will
        # receive as its in-memory analysis object.
        return analysis, paths

    except Exception as e:
        print(f"\n✗ Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        raise

    finally:
        es.close()


if __name__ == "__main__":
    # Example usage
    es_config = {
        "es_url": "https://your-es-url:9243",
        "api_key": "your-api-key",
    }

    output_dir = Path("output")

    # Run for all available sites
    for site_id in SITE_CONFIG.keys():
        try:
            print(f"\nProcessing {site_id}...")
            analysis, paths = run_analysis_pipeline(site_id, es_config, output_dir)

            print(f"\nResults for {site_id}:")
            print(f"  Excel: {paths['excel_file']}")
            print(f"  JSON: {paths['analysis_json']}")
            print(f"  OEE: {analysis['verified_kpis']['oee']:.2f}%")
            print(f"  Availability: {analysis['verified_kpis']['availability']:.2f}%\n")
        except Exception as e:
            print(f"Failed to process {site_id}: {e}\n")