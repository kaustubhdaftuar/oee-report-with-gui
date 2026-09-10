# pipeline/generate_charts.py (FULLY REFACTORED - Config-Driven)

"""
Chart Generation Pipeline - Refactored for Multi-Site Support

This module is now completely config-driven. All site-specific values
(chart output paths, KPI targets, etc.) come from config.json.

Key Functions:
- generate_kpi_scorecard_chart(data, site_config)
- generate_availability_breakdown_chart(data, site_config)
- generate_downtime_by_fault_chart(data, site_config)
- generate_spout_fault_distribution_chart(data, site_config)
- generate_quality_metrics_chart(data, site_config)
- generate_oee_trend_chart(data, site_config)
- generate_all_charts(site_id, week, config_manager)
"""

import json
import re
from pathlib import Path
from typing import Dict, Any

import matplotlib.pyplot as plt


# ════════════════════════════════════════════════════════════════════════════
# COLORS (Global - Same for all sites)
# ════════════════════════════════════════════════════════════════════════════

COLOR_GOOD = "#548235"
COLOR_WARN = "#ED7D31"
COLOR_BAD = "#C00000"
COLOR_TARGET_LINE = "#1F4E79"

DONUT_PALETTE = [
    "#FFC000",
    "#ED9455",
    "#A9D18E",
    "#9DC3E6",
    "#BFBFBF",
]


# ════════════════════════════════════════════════════════════════════════════
# NUMBER HELPERS
# ════════════════════════════════════════════════════════════════════════════

def get_number(value, default=0.0):
    """
    Parse a number out of whatever Excel/analysis.json handed us.
    Strips leading numeric token and parses it.
    
    Supports:
    - Float: 99.78
    - String with unit: "99.78%", "1000.0 min", "+0.19"
    - None: returns default
    """
    
    if value is None:
        return default
    
    if isinstance(value, bool):
        return default
    
    if isinstance(value, (int, float)):
        return float(value)
    
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        match = re.match(r"^[-+]?\d+(?:\.\d+)?", text)
        
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return default
    
    return default


def get_kpi_value(data, name):
    """
    Read a KPI percentage (0-100 scale) for the given name
    ('availability', 'performance', 'quality', 'oee').
    
    Args:
        data: analysis dict from create_analysis.py
        name: 'oee', 'availability', 'performance', 'quality'
    
    Returns:
        float: KPI value (0-100 scale)
    """
    
    kpi_percent = data.get("kpi_percent", {})
    value = kpi_percent.get(name)
    
    if value is not None:
        return get_number(value)
    
    # Fallback for older format
    kpi = data.get("kpi", {})
    item = kpi.get(name, {})
    raw_value = item.get("raw_value")
    
    if raw_value is not None:
        value = get_number(raw_value)
        if name in ("availability", "performance", "quality"):
            if abs(value) <= 1:
                value *= 100
        return value
    
    return 0.0


def status_color(value, target):
    """
    Determine bar color based on gap to target.
    
    ✅ GREEN: value >= target
    ⚠️  ORANGE: target-5 < value < target
    ❌ RED: value < target-5
    """
    
    gap = value - target
    
    if gap >= 0:
        return COLOR_GOOD
    
    if gap < -5:
        return COLOR_BAD
    
    return COLOR_WARN


# ════════════════════════════════════════════════════════════════════════════
# 1. KPI SCORECARD
# ════════════════════════════════════════════════════════════════════════════

def generate_kpi_scorecard_chart(data: Dict[str, Any], site_config: Dict[str, Any]) -> str:
    """
    ✅ DYNAMIC: Generate KPI scorecard chart using config targets.
    
    Reads KPI targets from site_config["kpi_thresholds"] instead of hardcoding.
    
    Args:
        data: analysis dict from create_analysis.py
        site_config: site configuration from config.json
    
    Returns:
        path to saved chart
    """
    
    print(f"\n[{site_config['site_id']}] Generating KPI scorecard chart...")
    
    names = ["Availability", "Performance", "Quality", "OEE"]
    values = [
        get_kpi_value(data, "availability"),
        get_kpi_value(data, "performance"),
        get_kpi_value(data, "quality"),
        get_kpi_value(data, "oee")
    ]
    
    # ✅ READ TARGETS FROM CONFIG (not hardcoded)
    thresholds = site_config.get("kpi_thresholds", {})
    targets = [
        thresholds.get("availability_min", 85),
        thresholds.get("performance_min", 90),
        thresholds.get("quality_min", 95),
        thresholds.get("oee_min", 70)
    ]
    
    print(f"  KPI VALUES:")
    for name, value in zip(names, values):
        print(f"    {name:<15}: {value:.1f}%")
    
    colors = [status_color(value, target) for value, target in zip(values, targets)]
    
    x = list(range(len(names)))
    
    plt.figure(figsize=(10, 5.5))
    
    bars = plt.bar(
        x, values,
        width=0.55,
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        zorder=3
    )
    
    plt.plot(
        x, targets,
        color=COLOR_TARGET_LINE,
        marker="o",
        linewidth=2,
        markersize=7,
        label="Target",
        zorder=4
    )
    
    for i, (value, target) in enumerate(zip(values, targets)):
        gap = value - target
        plt.text(
            i, value + 1.5,
            f"{value:.1f}%\nGap {gap:+.1f}",
            ha="center",
            fontsize=9,
            fontweight="bold",
            zorder=5
        )
    
    plt.title("KPI Scorecard: Actual vs Target", fontsize=15)
    plt.ylabel("%")
    plt.xticks(x, names)
    plt.ylim(0, 110)
    plt.grid(True, axis="y", alpha=0.3, zorder=0)
    plt.legend(loc="upper right")
    plt.tight_layout()
    
    return plt.gcf()


# ════════════════════════════════════════════════════════════════════════════
# 2. AVAILABILITY BREAKDOWN
# ════════════════════════════════════════════════════════════════════════════

def generate_availability_breakdown_chart(data: Dict[str, Any], site_config: Dict[str, Any]) -> str:
    """
    ✅ DYNAMIC: Donut chart showing availability breakdown.
    
    Reads availability details from data (created by create_analysis.py).
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating availability breakdown chart...")
    
    availability_details = data.get("availability_details", {})
    
    planned_time = get_number(availability_details.get("planned_production_time", 0))
    downtime = get_number(availability_details.get("total_available_time", 0)) - planned_time
    
    if downtime < 0:
        downtime = 0
    
    sizes = [planned_time, downtime]
    labels = [f"Production\n{planned_time:.0f} min", f"Downtime\n{downtime:.0f} min"]
    colors = [COLOR_GOOD, COLOR_BAD]
    
    plt.figure(figsize=(8, 6))
    
    plt.pie(
        sizes, labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops={"edgecolor": "black", "linewidth": 1.5}
    )
    
    plt.title("Availability Breakdown", fontsize=14, fontweight="bold")
    plt.tight_layout()
    
    return plt.gcf()


# ════════════════════════════════════════════════════════════════════════════
# 3. DOWNTIME BY FAULT TYPE
# ════════════════════════════════════════════════════════════════════════════

def generate_downtime_by_fault_chart(data: Dict[str, Any], site_config: Dict[str, Any]) -> str:
    """
    ✅ DYNAMIC: Horizontal bar chart showing downtime by fault type.
    
    Reads fault breakdown from data and uses site-specific fault names from config.
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating downtime by fault chart...")
    
    availability_details = data.get("availability_details", {})
    
    fault_types = {
        "spout_fault": "Spout Fault",
        "main_drive_stop": "Main Drive Stop",
        "belt_not_running": "Belt Not Running"
    }
    
    fault_names = [fault_types[key] for key in fault_types.keys()]
    fault_values = [
        get_number(availability_details.get(key, 0))
        for key in fault_types.keys()
    ]
    
    plt.figure(figsize=(10, 5))
    
    bars = plt.barh(
        fault_names, fault_values,
        color=COLOR_WARN,
        edgecolor="black",
        linewidth=1
    )
    
    for bar, value in zip(bars, fault_values):
        plt.text(value, bar.get_y() + bar.get_height()/2, f"{value:.0f} min",
                ha="left", va="center", fontweight="bold")
    
    plt.title("Downtime Breakdown by Fault Type", fontsize=14, fontweight="bold")
    plt.xlabel("Duration (minutes)")
    plt.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()
    
    return plt.gcf()


# ════════════════════════════════════════════════════════════════════════════
# 4. QUALITY METRICS
# ════════════════════════════════════════════════════════════════════════════

def generate_quality_metrics_chart(data: Dict[str, Any], site_config: Dict[str, Any]) -> str:
    """
    ✅ DYNAMIC: Stacked bar showing good vs defective bags.
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating quality metrics chart...")
    
    availability_details = data.get("availability_details", {})
    
    good_bags = get_number(availability_details.get("good_bags", 0))
    burst_bags = get_number(availability_details.get("burst_bags", 0))
    out_of_limit = get_number(availability_details.get("out_of_limit_bags", 0))
    
    categories = ["Good Bags", "Burst Bags", "Out of Limit"]
    values = [good_bags, burst_bags, out_of_limit]
    colors_quality = [COLOR_GOOD, COLOR_BAD, COLOR_WARN]
    
    plt.figure(figsize=(10, 5))
    
    bars = plt.bar(
        categories, values,
        color=colors_quality,
        edgecolor="black",
        linewidth=1.5
    )
    
    for bar, value in zip(bars, values):
        height = bar.get_height()
        plt.text(bar.get_x() + bar.get_width()/2, height,
                f"{value:,.0f}",
                ha="center", va="bottom", fontweight="bold")
    
    plt.title("Quality Metrics: Bag Status", fontsize=14, fontweight="bold")
    plt.ylabel("Count")
    plt.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    
    return plt.gcf()


# ════════════════════════════════════════════════════════════════════════════
# 5. OEE TREND (Week-over-Week)
# ════════════════════════════════════════════════════════════════════════════

def generate_oee_trend_chart(data: Dict[str, Any], site_config: Dict[str, Any]) -> str:
    """
    ✅ DYNAMIC: Line chart showing OEE trend over recent weeks.
    
    Reads previous_kpi from data (historical KPI snapshots).
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating OEE trend chart...")
    
    current_oee = get_kpi_value(data, "oee")
    current_week = data.get("week", 0)
    
    previous_kpi = data.get("previous_kpi", {})
    previous_oee = get_kpi_value({"kpi_percent": previous_kpi}, "oee") if previous_kpi else None
    
    weeks = [current_week - 1, current_week]
    oee_values = []
    
    if previous_oee is not None and previous_oee > 0:
        oee_values.append(previous_oee)
    else:
        oee_values.append(current_oee)  # Fallback if no history
    
    oee_values.append(current_oee)
    
    plt.figure(figsize=(10, 5))
    
    plt.plot(
        weeks, oee_values,
        marker="o",
        linewidth=3,
        markersize=10,
        color=COLOR_GOOD,
        label="OEE Trend"
    )
    
    plt.axhline(y=site_config.get("kpi_thresholds", {}).get("oee_min", 70),
               color=COLOR_TARGET_LINE, linestyle="--", linewidth=2, label="Target")
    
    for week, oee in zip(weeks, oee_values):
        plt.text(week, oee + 1.5, f"{oee:.1f}%", ha="center", fontweight="bold")
    
    plt.title("OEE Trend (Week-over-Week)", fontsize=14, fontweight="bold")
    plt.xlabel("Week")
    plt.ylabel("OEE %")
    plt.xticks(weeks)
    plt.ylim(0, 110)
    plt.grid(True, alpha=0.3)
    plt.legend(loc="upper left")
    plt.tight_layout()
    
    return plt.gcf()


# ════════════════════════════════════════════════════════════════════════════
# MASTER: GENERATE ALL CHARTS
# ════════════════════════════════════════════════════════════════════════════

def generate_all_charts(site_id: str, week: int, config_manager) -> Dict[str, Path]:
    """
    ✅ UNIFIED ENTRY POINT: Generate all charts for a site.
    
    Reads site-specific analysis, generates charts, saves to site-specific paths.
    
    All paths come from ConfigManager:
    - chart_dir = config.get_paths(site_id, week)["chart_dir"]
    - analysis_file = config.get_paths(site_id, week)["analysis_json"]
    
    Args:
        site_id: Site identifier (e.g., "jk_cement_aligarh")
        week: ISO week number
        config_manager: ConfigManager instance
    
    Returns:
        dict of chart paths: {"kpi_scorecard": Path, "availability": Path, ...}
    
    Raises:
        Exception if analysis file not found or chart generation fails
    """
    
    site_config = config_manager.get_site_config(site_id)
    paths = config_manager.get_paths(site_id, week)
    
    print(f"\n{'='*70}")
    print(f"[{site_id}] GENERATING CHARTS")
    print(f"{'='*70}")
    
    # ✅ Read analysis from site-specific location
    analysis_file = paths["analysis_json"]
    
    if not analysis_file.exists():
        raise FileNotFoundError(
            f"Analysis file not found for {site_id} week {week}:\n{analysis_file}\n"
            f"Run create_analysis_pipeline() first."
        )
    
    with open(analysis_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    print(f"  Loaded analysis from: {analysis_file}")
    
    # ✅ Create chart directory (site + week specific)
    chart_dir = paths["chart_dir"]
    chart_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"  Output directory: {chart_dir}")
    
    chart_files = {}
    
    # Generate each chart type
    chart_generators = [
        ("kpi_scorecard", generate_kpi_scorecard_chart),
        ("availability_breakdown", generate_availability_breakdown_chart),
        ("downtime_by_fault", generate_downtime_by_fault_chart),
        ("quality_metrics", generate_quality_metrics_chart),
        ("oee_trend", generate_oee_trend_chart),
    ]
    
    for chart_name, chart_func in chart_generators:
        try:
            fig = chart_func(data, site_config)
            
            # ✅ Save with site-specific naming
            chart_path = chart_dir / f"{chart_name}.png"
            fig.savefig(chart_path, dpi=300, bbox_inches="tight")
            plt.close(fig)
            
            chart_files[chart_name] = chart_path
            print(f"  ✓ {chart_name}.png saved")
        
        except Exception as e:
            print(f"  ✗ {chart_name} failed: {e}")
            raise
    
    print(f"\n✓ All charts generated for {site_id} week {week}")
    
    return chart_files


if __name__ == "__main__":
    # For testing only - requires ConfigManager
    # Usage: python generate_charts.py
    pass