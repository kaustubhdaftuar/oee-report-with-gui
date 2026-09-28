# pipeline/generate_charts.py (FULLY REFACTORED - Config-Driven)

"""
Chart Generation Pipeline - Refactored for Multi-Site Support

This module is now completely config-driven. All site-specific values
(chart output paths, KPI targets, etc.) come from config.json.

Key Functions:
- generate_kpi_scorecard_chart(data, site_config)
- generate_availability_chart(data, site_config)
- generate_downtime_by_fault_chart(data, site_config)
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
    - Dict with raw_value: {"raw_value": 99.78, ...}
    - None: returns default
    """
    
    if value is None:
        return default
    
    if isinstance(value, bool):
        return default
    
    if isinstance(value, dict):
        # Handle cell_info structure
        value = value.get("raw_value", default)
        if value is None:
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
    
    ✅ FIXED: Handles both flat float values and nested dict structures.
    
    Args:
        data: analysis dict from create_analysis.py
        name: 'oee', 'availability', 'performance', 'quality'
    
    Returns:
        float: KPI value (0-100 scale)
    """
    
    # PRIMARY: Try kpi_percent (flat structure with float values)
    kpi_percent = data.get("kpi_percent", {})
    value = kpi_percent.get(name)
    
    if value is not None:
        return get_number(value)
    
    # FALLBACK: Try nested kpi structure (dict with "raw_value" key)
    kpi = data.get("kpi", {})
    item = kpi.get(name, {})
    
    # Check if item is a dict with raw_value
    if isinstance(item, dict):
        raw_value = item.get("raw_value")
        
        if raw_value is not None:
            value = get_number(raw_value)
            # Scale up if value is 0-1 (should be 0-100)
            if name in ("availability", "performance", "quality"):
                if abs(value) <= 1:
                    value *= 100
            return value
    
    # If item is already a number (float), use it directly
    elif isinstance(item, (int, float)):
        value = get_number(item)
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
        matplotlib figure
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
# 2. AVAILABILITY BREAKDOWN (Donut)
# ════════════════════════════════════════════════════════════════════════════

def generate_availability_breakdown_chart(data: Dict[str, Any], site_config: Dict[str, Any]) -> str:
    """
    ✅ DYNAMIC: Donut chart showing availability breakdown.
    
    Reads availability details from data using _min suffixes.
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating availability breakdown chart...")
    
    availability_details = data.get("availability_details", {})
    
    # ✅ Use _min suffix (actual data structure)
    planned_time = get_number(availability_details.get("planned_production_time_min", 0))
    total_available = get_number(availability_details.get("total_available_time_min", 0))
    
    downtime = total_available - planned_time
    if downtime < 0:
        downtime = 0
    
    # Guard against zero-sized pie chart
    if planned_time <= 0 and downtime <= 0:
        print(f"  ⚠ No availability data; skipping chart")
        fig = plt.figure(figsize=(8, 6))
        plt.text(0.5, 0.5, "Insufficient availability data",
                ha="center", va="center", fontsize=12, 
                transform=plt.gca().transAxes)
        plt.axis("off")
        plt.tight_layout()
        return fig
    
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
    
    Reads fault breakdown from downtime_breakdown_by_plc and aggregates across PLCs.
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating downtime by fault chart...")
    
    breakdown = data.get("downtime_breakdown_by_plc", {})
    
    if not breakdown:
        print(f"  ⚠ No PLC downtime breakdown found; skipping chart")
        fig = plt.figure(figsize=(10, 5))
        plt.text(0.5, 0.5, "No downtime data available",
                ha="center", va="center", fontsize=12,
                transform=plt.gca().transAxes)
        plt.axis("off")
        plt.tight_layout()
        return fig
    
    # Map fault keys to labels
    fault_key_to_label = {
        "spout_fault_min": "Spout Fault",
        "main_drive_stop_min": "Main Drive Stop",
        "belt_not_running_min": "Belt Not Running",
    }
    
    # Aggregate across all PLCs
    fault_totals = {}
    
    for plc_entry in breakdown.values():
        for key, value in plc_entry.items():
            if key != "machine":
                label = fault_key_to_label.get(key, key.replace("_min", "").replace("_", " ").title())
                num_value = get_number(value)
                fault_totals[label] = fault_totals.get(label, 0) + num_value
    
    if not fault_totals or not any(v > 0 for v in fault_totals.values()):
        print(f"  ⚠ No fault duration values; skipping chart")
        fig = plt.figure(figsize=(10, 5))
        plt.text(0.5, 0.5, "No fault data available",
                ha="center", va="center", fontsize=12,
                transform=plt.gca().transAxes)
        plt.axis("off")
        plt.tight_layout()
        return fig
    
    fault_names = list(fault_totals.keys())
    fault_values = list(fault_totals.values())
    
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
    ✅ DYNAMIC: Donut chart showing quality breakdown.
    
    Reads quality details (good bags, burst bags, out of limit) from data.
    
    Args:
        data: analysis dict
        site_config: site configuration
    
    Returns:
        matplotlib figure
    """
    
    print(f"\n[{site_config['site_id']}] Generating quality metrics chart...")
    
    quality_details = data.get("quality_details", {})
    
    quality_items = [
        ("Out of Limit Bags", quality_details.get("out_of_limit_bags")),
        ("Burst Bags", quality_details.get("burst_bags")),
        ("Good Bags", quality_details.get("good_bags")),
    ]
    
    contributors = []
    
    for reason, value in quality_items:
        number = get_number(value)
        if number > 0:
            contributors.append({"reason": reason, "bags": number})
    
    if not contributors:
        print(f"  ⚠ No quality data; skipping chart")
        fig = plt.figure(figsize=(10, 5))
        plt.text(0.5, 0.5, "No quality data available",
                ha="center", va="center", fontsize=12,
                transform=plt.gca().transAxes)
        plt.axis("off")
        plt.tight_layout()
        return fig
    
    # Sort by bag count descending
    contributors.sort(key=lambda x: x["bags"], reverse=True)
    
    # Largest loss category first (for title)
    loss_categories = [c for c in contributors if "Good" not in c["reason"]]
    largest_loss = loss_categories[0]["reason"] if loss_categories else "Quality Loss"
    
    labels = [c["reason"] for c in contributors]
    sizes = [c["bags"] for c in contributors]
    colors = [COLOR_GOOD if "Good" in l else COLOR_BAD if "Burst" in l else COLOR_WARN 
              for l in labels]
    
    plt.figure(figsize=(10, 6))
    
    plt.pie(
        sizes, labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops={"edgecolor": "black", "linewidth": 1.5}
    )
    
    plt.title(f"Quality Metrics: {largest_loss}", fontsize=14, fontweight="bold")
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
# 6. SPOUT DOWNTIME BY MACHINE (one chart per PLC)
# ════════════════════════════════════════════════════════════════════════════

def generate_spout_downtime_chart(spout_data: Dict[Any, Any], plc_label: str):
    """
    Horizontal bar chart of per-spout downtime minutes for one PLC.

    Args:
        spout_data: {spout_number: downtime_minutes, ...} — as saved in
            analysis.json's spout_downtime[plc_id]. Keys come back as
            strings after a JSON round-trip; coerced to int here for
            sorting/display.
        plc_label: display name for the chart title (e.g. "Fillpac 1").

    Returns:
        matplotlib figure. If spout_data is empty, returns a placeholder
        "No spout downtime recorded" figure instead of an empty plot —
        this is what makes a PLC with zero spout faults this week still
        render a real (if blank) image instead of leaving the report's
        image placeholder untouched.
    """
    if not spout_data:
        fig = plt.figure(figsize=(10, 5))
        plt.text(0.5, 0.5, "No spout downtime recorded",
                  ha="center", va="center", fontsize=12,
                  transform=plt.gca().transAxes)
        plt.axis("off")
        plt.tight_layout()
        return fig

    items = sorted(
        ((int(spout), get_number(minutes)) for spout, minutes in spout_data.items()),
        key=lambda item: item[0],
    )
    labels = [f"Spout {spout}" for spout, _ in items]
    values = [minutes for _, minutes in items]

    plt.figure(figsize=(10, max(4, 0.35 * len(items))))

    bars = plt.barh(labels, values, color=COLOR_WARN, edgecolor="black", linewidth=1)

    for bar, value in zip(bars, values):
        plt.text(
            value, bar.get_y() + bar.get_height() / 2, f"{value:.1f} min",
            ha="left", va="center", fontweight="bold", fontsize=8,
        )

    plt.title(f"Spout Downtime — {plc_label}", fontsize=13, fontweight="bold")
    plt.xlabel("Duration (minutes)")
    plt.gca().invert_yaxis()  # Spout 1 at top
    plt.grid(True, axis="x", alpha=0.3)
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
    
    # --------------------------------------------------------------------
    # Spout downtime — one chart per PLC (not part of the uniform
    # single-figure-per-name loop above, since it produces N files, one
    # per PLC configured for this site, rather than exactly one).
    # --------------------------------------------------------------------
    spout_downtime_data = data.get("spout_downtime", {})

    plc_machine_names = {
        plc_id: entry.get("machine", plc_id)
        for plc_id, entry in data.get("downtime_breakdown_by_plc", {}).items()
    }

    for plc_id, spout_data in spout_downtime_data.items():
        chart_key = f"spout_downtime_{plc_id.lower()}"

        try:
            plc_label = plc_machine_names.get(plc_id, plc_id)
            fig = generate_spout_downtime_chart(spout_data, plc_label)

            chart_path = chart_dir / f"{chart_key}.png"
            fig.savefig(chart_path, dpi=300, bbox_inches="tight")
            plt.close(fig)

            chart_files[chart_key] = chart_path
            print(f"  ✓ {chart_key}.png saved")

        except Exception as e:
            print(f"  ✗ {chart_key} failed: {e}")
            raise

    print(f"\n✓ All charts generated for {site_id} week {week}")
    
    return chart_files


if __name__ == "__main__":
    # For testing only - requires ConfigManager
    # Usage: python generate_charts.py
    pass