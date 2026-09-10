"""
Local LLM Client - Generates OEE insights from verified analysis data.

Uses a local LLM endpoint (e.g., TinyLlama, Llama2, etc.) to generate
management commentary on OEE performance based on verified metrics.

Replaces Gemini with a local inference server you can configure via config.json.

Usage:
  from pipeline.local_llm_client import generate_insights
  
  insights = generate_insights(
      analysis_data,
      config_manager,
      progress_callback=lambda msg: print(msg)
  )
"""

import json
import re
import logging
from typing import Dict, Any, Optional, Callable
from pathlib import Path

import requests

from .prompts import REPORT_SYSTEM_PROMPT, REPORT_INSTRUCTIONS

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

MAX_NEW_TOKENS = 400
REQUEST_TIMEOUT_SECONDS = 60


# ============================================================
# UTILITY
# ============================================================

def _raw(value, default=0.0):
    """Unwrap a cell_info dict ({"raw_value": ...}) to a float."""
    if isinstance(value, dict):
        value = value.get("raw_value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _kpi_percent(analysis, name):
    """
    Read a KPI percentage from analysis["kpi"][name].
    OEE is already a percentage; others are decimal fractions → *100.
    """
    item = analysis.get("kpi", {}).get(name, {})
    value = _raw(item, default=None)
    
    if value is None:
        return 0.0
    
    if name in ("availability", "performance", "quality") and abs(value) <= 1:
        value *= 100
    
    return value


# KPI targets for status labeling
KPI_TARGETS = {
    "Availability": 90.0,
    "Performance": 95.0,
    "Quality": 99.9,
    "OEE": 85.0,
}


# ============================================================
# EXTRACT VERIFIED FACTS
# ============================================================

def extract_facts(data):
    """
    Extract verified facts from analysis JSON.
    Mirrors the logic from create_analysis.py to ensure consistency.
    """
    
    machine = data.get("machine", "Unknown")
    current_week = data.get("week", "N/A")
    previous_week = data.get("previous_week", "N/A")
    
    oee = _kpi_percent(data, "oee")
    availability = _kpi_percent(data, "availability")
    performance = _kpi_percent(data, "performance")
    quality = _kpi_percent(data, "quality")
    
    if oee >= KPI_TARGETS["OEE"]:
        oee_status = "On Target"
    elif oee >= KPI_TARGETS["OEE"] - 15:
        oee_status = "Below Target"
    else:
        oee_status = "Critical"
    
    kpi_rank = [
        {"name": "Availability", "value": availability},
        {"name": "Performance", "value": performance},
        {"name": "Quality", "value": quality},
    ]
    
    weakest_kpi = min(kpi_rank, key=lambda item: item["value"])
    strongest_kpi = max(kpi_rank, key=lambda item: item["value"])
    
    # Downtime analysis
    availability_details = data.get("availability_details", {})
    planned_time = _raw(availability_details.get("planned_production_time_min"))
    available_time = _raw(availability_details.get("total_available_time_min"))
    availability_loss = max(planned_time - available_time, 0)
    
    downtime_items = [
        ("Belt Not Running", _raw(availability_details.get("belt_not_running_min"))),
        ("E-Stop", _raw(availability_details.get("e_stop_min"))),
        ("Motor Trip", _raw(availability_details.get("motor_trip_min"))),
        ("RPM Change", _raw(availability_details.get("rpm_change_min"))),
    ]
    downtime_items.sort(key=lambda item: item[1], reverse=True)
    
    main_downtime_label, main_downtime_minutes = (
        downtime_items[0] if downtime_items and downtime_items[0][1] > 0 else ("N/A", 0.0)
    )
    
    # Quality analysis
    quality_details = data.get("quality_details", {})
    good_bags = _raw(quality_details.get("good_bags"))
    
    quality_loss_items = [
        ("Burst Bags", _raw(quality_details.get("burst_bags"))),
        ("Out of Limit Bags", _raw(quality_details.get("out_of_limit_bags"))),
        ("Tearing Fault", _raw(quality_details.get("tearing_fault"))),
    ]
    quality_loss_items.sort(key=lambda item: item[1], reverse=True)
    quality_loss_bags = sum(bags for _, bags in quality_loss_items)
    total_produced_bags = good_bags + quality_loss_bags
    
    main_quality_label, main_quality_bags = (
        quality_loss_items[0] if quality_loss_items and quality_loss_items[0][1] > 0 else ("N/A", 0.0)
    )
    
    return {
        "machine": machine,
        "current_week": current_week,
        "previous_week": previous_week,
        "oee": oee,
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee_status": oee_status,
        "weakest_kpi": weakest_kpi["name"],
        "strongest_kpi": strongest_kpi["name"],
        "planned_production_time_min": planned_time,
        "total_available_time_min": available_time,
        "total_availability_loss_min": availability_loss,
        "main_downtime_loss": main_downtime_label,
        "main_downtime_minutes": main_downtime_minutes,
        "good_bags": good_bags,
        "quality_loss_bags": quality_loss_bags,
        "total_produced_bags": total_produced_bags,
        "main_quality_loss": main_quality_label,
        "main_quality_bags": main_quality_bags,
    }


# ============================================================
# BUILD PROMPT
# ============================================================

def build_prompt(facts):
    """Build the prompt for the LLM based on verified facts."""
    
    def format_fact(value):
        """Format a number for display."""
        if isinstance(value, str):
            return value
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)
    
    prompt = f"""
{REPORT_INSTRUCTIONS}

VERIFIED PRODUCTION DATA FOR THIS ANALYSIS:

Machine: {format_fact(facts['machine'])}
Current Week: {format_fact(facts['current_week'])}
Previous Week: {format_fact(facts['previous_week'])}

KPI Performance:
- OEE: {format_fact(facts['oee'])}% ({facts['oee_status']})
- Availability: {format_fact(facts['availability'])}%
- Performance: {format_fact(facts['performance'])}%
- Quality: {format_fact(facts['quality'])}%

Weakest KPI: {facts['weakest_kpi']}
Strongest KPI: {facts['strongest_kpi']}

Downtime Analysis:
- Planned Production Time: {format_fact(facts['planned_production_time_min'])} minutes
- Total Available Time: {format_fact(facts['total_available_time_min'])} minutes
- Total Availability Loss: {format_fact(facts['total_availability_loss_min'])} minutes
- Main Downtime Loss: {facts['main_downtime_loss']} ({format_fact(facts['main_downtime_minutes'])} minutes)

Quality Analysis:
- Good Bags: {format_fact(facts['good_bags'])}
- Quality Loss Bags: {format_fact(facts['quality_loss_bags'])}
- Total Produced Bags: {format_fact(facts['total_produced_bags'])}
- Main Quality Loss: {facts['main_quality_loss']} ({format_fact(facts['main_quality_bags'])} bags)
"""
    
    return prompt


# ============================================================
# CALL LOCAL LLM
# ============================================================

def call_local_llm(prompt: str, config_manager) -> str:
    """
    Call the local LLM endpoint configured in config.json.
    
    Config should have:
    {
        "llm": {
            "provider": "local",
            "endpoint": "http://localhost:8000",
            "model_name": "tinyllama",
            "timeout": 300
        }
    }
    """
    
    llm_config = config_manager.get_llm_config()
    
    if not llm_config or llm_config.get("provider") != "local":
        logger.warning("LLM not configured or not set to 'local' provider. Using fallback commentary.")
        return ""
    
    endpoint = llm_config.get("endpoint")
    model_name = llm_config.get("model_name")
    timeout = llm_config.get("timeout", REQUEST_TIMEOUT_SECONDS)
    
    if not endpoint:
        logger.warning("LLM endpoint not configured. Using fallback commentary.")
        return ""
    
    try:
        logger.info(f"Calling local LLM: {endpoint} (model: {model_name})")
        
        # Common local LLM API formats
        # Adjust based on your server (Ollama, LM Studio, vLLM, etc.)
        
        payload = {
            "prompt": prompt,
            "max_tokens": MAX_NEW_TOKENS,
            "temperature": 0.7,
            "top_p": 0.95,
            "stop": ["END_OF_RESPONSE"]
        }
        
        response = requests.post(
            endpoint,
            json=payload,
            timeout=timeout,
            verify=False  # For self-signed certs
        )
        
        response.raise_for_status()
        
        result = response.json()
        
        # Parse response based on common formats
        if "text" in result:
            return result["text"].strip()
        elif "choices" in result and result["choices"]:
            return result["choices"][0].get("text", "").strip()
        elif "response" in result:
            return result["response"].strip()
        
        logger.warning(f"Unexpected LLM response format: {result.keys()}")
        return ""
    
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Cannot connect to LLM endpoint ({endpoint}): {e}")
        logger.info("Using fallback commentary (no AI insights)")
        return ""
    except requests.exceptions.Timeout as e:
        logger.error(f"LLM request timed out: {e}")
        return ""
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return ""


# ============================================================
# CLEAN RESPONSE
# ============================================================

def extract_sections(response: str) -> dict:
    """
    Extract labeled sections from LLM response.
    Looks for: SECTION LABEL: content
    """
    sections = {}
    labels = [
        "EXECUTIVE SUMMARY",
        "MAIN ISSUE",
        "DOWNTIME ACTION",
        "QUALITY ACTION",
    ]
    
    for label in labels:
        pattern = rf"{label}:\s*(.+?)(?=\n[A-Z][A-Z ]+:|$)"
        match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
        if match:
            sections[label.lower().replace(" ", "_")] = match.group(1).strip()
    
    return sections


# ============================================================
# BUILD INSIGHTS
# ============================================================

def build_insights(facts, llm_response):
    """
    Build final insights JSON combining verified facts and AI commentary.
    Falls back to Python-generated commentary if LLM fails.
    """
    
    sections = extract_sections(llm_response)
    
    # Fallback commentary if LLM didn't provide
    if not sections:
        logger.info("No LLM response. Using fallback commentary.")
        sections = {
            "executive_summary": (
                f"OEE for {facts['machine']} week {facts['current_week']} was "
                f"{facts['oee']:.2f}%, with {facts['weakest_kpi']} as the weakest KPI "
                f"({facts['weakest_kpi'].lower()}: {facts.get('availability', facts.get('performance', facts.get('quality', 0))):.2f}%)."
            ),
            "main_issue": (
                f"The largest downtime contributor was {facts['main_downtime_loss']} "
                f"({facts['main_downtime_minutes']:.2f} minutes)."
            ),
            "downtime_action": (
                f"Investigate and address {facts['main_downtime_loss']} to reduce downtime."
            ),
            "quality_action": (
                f"Address {facts['main_quality_loss']} ({facts['main_quality_bags']:.0f} bags) "
                f"to improve quality performance."
            ),
        }
    
    return {
        "success": True,
        "ai_engine": "Local LLM",
        "mode": "LOCAL INFERENCE",
        "machine": facts["machine"],
        "current_week": facts["current_week"],
        "previous_week": facts["previous_week"],
        
        "verified_kpis": {
            "oee": facts["oee"],
            "availability": facts["availability"],
            "performance": facts["performance"],
            "quality": facts["quality"],
        },
        
        "verified_status": {
            "oee_status": facts["oee_status"],
            "weakest_kpi": facts["weakest_kpi"],
            "strongest_kpi": facts["strongest_kpi"],
        },
        
        "ai_insights": {
            "executive_summary": sections.get("executive_summary", ""),
            "main_issue": sections.get("main_issue", ""),
            "downtime_action": sections.get("downtime_action", ""),
            "quality_action": sections.get("quality_action", ""),
        },
        
        # Flat fields for report template
        "executive_summary": sections.get("executive_summary", ""),
        "oee_analysis": f"OEE was {facts['oee']:.2f}% ({facts['oee_status']}).",
        "main_issue": sections.get("main_issue", ""),
        "downtime_action": sections.get("downtime_action", ""),
        "quality_action": sections.get("quality_action", ""),
        
        "raw_model_response": llm_response,
    }


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def generate_insights(
    analysis: Dict[str, Any],
    config_manager,
    progress_callback: Optional[Callable[[str], None]] = None
) -> Dict[str, Any]:
    """
    Generate OEE insights from verified analysis data.
    
    Args:
        analysis: Output from create_analysis.py
        config_manager: ConfigManager instance (for LLM config)
        progress_callback: Optional callback for progress messages
    
    Returns:
        Insights JSON with verified facts and AI commentary
    """
    
    def emit(msg):
        if progress_callback:
            progress_callback(msg)
        logger.info(msg)
    
    try:
        emit("Extracting verified facts from analysis...")
        facts = extract_facts(analysis)
        
        emit("Building prompt for LLM...")
        prompt = build_prompt(facts)
        
        emit("Calling local LLM for insights...")
        llm_response = call_local_llm(prompt, config_manager)
        
        emit("Building final insights JSON...")
        insights = build_insights(facts, llm_response)
        
        emit("✓ Insights generated successfully")
        return insights
    
    except Exception as e:
        logger.error(f"Failed to generate insights: {e}")
        # Return fallback insights on error
        facts = extract_facts(analysis)
        return build_insights(facts, "")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    print(__doc__)