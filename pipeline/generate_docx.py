# pipeline/generate_docx.py (FULLY REFACTORED - Config-Driven)

"""
DOCX Report Generation Pipeline - Refactored for Multi-Site Support

This module is now completely config-driven. All site-specific values
(template paths, output directories, placeholder names) come from config.json.

Key Functions:
- load_json(file_path): Load JSON safely
- get_number(value, default): Parse numbers from Excel strings
- replace_placeholder(doc, placeholder_name, replacement_text, bold, size):
    Replace text in DOCX by placeholder name
- insert_chart_images(doc, site_id, week, config_manager): Insert generated charts
- convert_docx_to_pdf(docx_path, pdf_path): Convert DOCX → PDF
- generate_report_docx(site_id, week, config_manager): Main entry point
"""

import json
import re
import platform
import subprocess
from pathlib import Path
from copy import deepcopy
from datetime import datetime

from docx import Document
from docx.shared import Pt, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.table import _Row
from docx.text.paragraph import Paragraph
from docx.image.image import Image as DocxImage


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def load_json(file_path: Path) -> dict:
    """
    Load JSON file safely. Raises error if not found.
    
    Args:
        file_path: Path to JSON file
    
    Returns:
        dict: Parsed JSON
    
    Raises:
        FileNotFoundError: If file doesn't exist
    """
    
    if not file_path.exists():
        raise FileNotFoundError(f"File not found:\n{file_path}")
    
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def get_number(value, default=0.0) -> float:
    """
    Parse a number out of whatever analysis.json handed us.
    Handles TEXT with units like "99.78%", "1000.0 min", "+0.19 pp"
    
    Args:
        value: Value to parse (can be float, string, None)
        default: Default if parsing fails
    
    Returns:
        float: Parsed number
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


def percentage(value) -> str:
    """Format number as percentage string."""
    return f"{get_number(value):.2f}%"


def minutes(value) -> str:
    """Format number as minutes string."""
    return f"{get_number(value):.2f} min"


def bags(value) -> str:
    """Format number as bags string with comma separators."""
    return f"{get_number(value):,.0f}"


def change_pp(value) -> str:
    """Format number as percentage point change (±X.XX pp)."""
    val = get_number(value)
    
    if val > 0:
        return f"+{val:.2f} pp"
    
    return f"{val:.2f} pp"


def direction(value) -> str:
    """Return human-readable direction for a KPI delta."""
    val = get_number(value)
    
    if val > 0.05:
        return "Improved"
    if val < -0.05:
        return "Decreased"
    
    return "No Change"


def clear_paragraph(paragraph):
    """Remove all runs from a paragraph."""
    for run in paragraph.runs:
        run._element.getparent().remove(run._element)


def replace_paragraph_text(paragraph, text: str, bold=False, size=None):
    """
    Replace paragraph text while preserving formatting.
    
    Args:
        paragraph: python-docx paragraph object
        text: New text to insert
        bold: Make text bold
        size: Font size in points
    """
    
    clear_paragraph(paragraph)
    
    run = paragraph.add_run(text)
    
    if bold:
        run.bold = True
    
    if size:
        run.font.size = Pt(size)


def set_cell_text(cell, text: str, bold=False, size=None):
    """Set cell text in a table."""
    clear_paragraph(cell.paragraphs[0])
    run = cell.paragraphs[0].add_run(text)
    
    if bold:
        run.bold = True
    
    if size:
        run.font.size = Pt(size)


# ════════════════════════════════════════════════════════════════════════════
# PLACEHOLDER REPLACEMENT
# ════════════════════════════════════════════════════════════════════════════

def replace_placeholder_in_paragraphs(doc, placeholder_name: str, replacement_text: str):
    """
    Replace all occurrences of a placeholder in document paragraphs.
    
    ✅ DYNAMIC: Works with any placeholder name from config.
    
    Args:
        doc: python-docx Document object
        placeholder_name: Placeholder like "{{OEE}}", "{{AVAILABILITY}}"
        replacement_text: Text to insert
    """
    
    for paragraph in doc.paragraphs:
        if placeholder_name in paragraph.text:
            replace_paragraph_text(paragraph, replacement_text)


def replace_placeholder_in_tables(doc, placeholder_name: str, replacement_text: str):
    """Replace all occurrences of a placeholder in table cells."""
    
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if placeholder_name in cell.text:
                    set_cell_text(cell, replacement_text)


def replace_placeholder(doc, placeholder_name: str, replacement_text: str):
    """
    Replace all occurrences of a placeholder throughout document.
    
    ✅ DYNAMIC: Works in paragraphs AND tables.
    
    Args:
        doc: python-docx Document object
        placeholder_name: Placeholder like "{{OEE}}" or "{{AVAILABILITY}}"
        replacement_text: Text to insert
    """
    
    replace_placeholder_in_paragraphs(doc, placeholder_name, replacement_text)
    replace_placeholder_in_tables(doc, placeholder_name, replacement_text)


# ════════════════════════════════════════════════════════════════════════════
# CHART INSERTION
# ════════════════════════════════════════════════════════════════════════════

def insert_chart_images(doc, site_id: str, week: int, config_manager):
    """
    ✅ DYNAMIC: Insert generated charts into document.
    
    Reads chart directory from ConfigManager.
    Inserts each chart PNG file found in the directory.
    
    Chart naming convention (from generate_charts.py):
    - kpi_scorecard.png
    - availability_breakdown.png
    - downtime_by_fault.png
    - quality_metrics.png
    - oee_trend.png
    
    Args:
        doc: python-docx Document object
        site_id: Site identifier
        week: ISO week number
        config_manager: ConfigManager instance
    """
    
    paths = config_manager.get_paths(site_id, week)
    chart_dir = paths["chart_dir"]
    
    if not chart_dir.exists():
        print(f"  ⚠ Chart directory not found: {chart_dir}")
        return
    
    # Find all PNG files in chart directory
    chart_files = sorted(chart_dir.glob("*.png"))
    
    if not chart_files:
        print(f"  ⚠ No charts found in: {chart_dir}")
        return
    
    print(f"  Found {len(chart_files)} charts to insert")
    
    # Add paragraph before inserting charts
    doc.add_paragraph("Charts & Visualizations:", style="Heading 2")
    
    for chart_file in chart_files:
        try:
            # Add chart file name as subheading
            chart_name = chart_file.stem.replace("_", " ").title()
            doc.add_paragraph(chart_name, style="Heading 3")
            
            # Add image with consistent width
            doc.add_picture(str(chart_file), width=Emu(5500000))  # 5.5 inches
            
            print(f"    ✓ Inserted {chart_file.name}")
        
        except Exception as e:
            print(f"    ✗ Failed to insert {chart_file.name}: {e}")


# ════════════════════════════════════════════════════════════════════════════
# PDF CONVERSION
# ════════════════════════════════════════════════════════════════════════════

def convert_docx_to_pdf(docx_path: Path, pdf_path: Path):
    """
    ✅ DYNAMIC: Convert DOCX to PDF using LibreOffice or equivalent.
    
    Works on Windows, macOS, and Linux.
    Requires LibreOffice to be installed.
    
    Args:
        docx_path: Path to .docx file
        pdf_path: Path to output .pdf file
    
    Raises:
        RuntimeError: If conversion fails
    """
    
    if not docx_path.exists():
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")
    
    print(f"  Converting DOCX → PDF...")
    print(f"    Input: {docx_path}")
    print(f"    Output: {pdf_path}")
    
    try:
        # Try LibreOffice first (cross-platform)
        if platform.system() == "Windows":
            soffice_paths = [
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            ]
            soffice = None
            for path in soffice_paths:
                if Path(path).exists():
                    soffice = path
                    break
        else:
            # macOS / Linux
            soffice = "libreoffice"
        
        if not soffice:
            raise RuntimeError("LibreOffice not found. Install it to enable PDF conversion.")
        
        # Run LibreOffice conversion
        cmd = [
            soffice,
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(pdf_path.parent),
            str(docx_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")
        
        # Verify PDF was created
        if not pdf_path.exists():
            raise RuntimeError("PDF file was not created by LibreOffice")
        
        print(f"  ✓ PDF conversion successful")
    
    except Exception as e:
        print(f"  ✗ PDF conversion failed: {e}")
        raise


# ════════════════════════════════════════════════════════════════════════════
# MAIN: GENERATE REPORT DOCX
# ════════════════════════════════════════════════════════════════════════════

def generate_report_docx(site_id: str, week: int, config_manager, status_callback=None) -> Path:
    """
    ✅ UNIFIED ENTRY POINT: Generate complete DOCX report for a site.
    
    All paths and configurations come from ConfigManager:
    - template_path = config["template_path"]
    - analysis_file = ConfigManager.get_paths()["analysis_json"]
    - output_file = ConfigManager.get_paths()["report_docx"]
    
    Steps:
    1. Load template from site-specific path (from config.json)
    2. Load analysis from site-specific analysis JSON
    3. Replace all placeholders with site-specific values
    4. Insert generated chart images
    5. Save DOCX
    6. Convert to PDF (optional)
    
    Args:
        site_id: Site identifier (e.g., "jk_cement_aligarh")
        week: ISO week number
        config_manager: ConfigManager instance
        status_callback: Optional function(message) for progress updates
    
    Returns:
        Path to generated report PDF
    
    Raises:
        Exception if template, analysis, or conversion fails
    """
    
    def emit(message):
        if status_callback:
            status_callback(message)
        print(f"[{site_id}] {message}")
    
    emit("Initializing report generation...")
    
    site_config = config_manager.get_site_config(site_id)
    paths = config_manager.get_paths(site_id, week)
    
    # ══════════════════════════════════════════════════════════════════════
    # STEP 1: Load template (site-specific from config.json)
    # ══════════════════════════════════════════════════════════════════════
    
    emit("Loading template...")
    
    template_path = Path(site_config["template_path"])
    
    if not template_path.exists():
        raise FileNotFoundError(
            f"Template not found for {site_id}:\n{template_path}\n"
            f"Check config.json template_path value."
        )
    
    doc = Document(str(template_path))
    emit(f"  Template loaded: {template_path.name}")
    
    # ══════════════════════════════════════════════════════════════════════
    # STEP 2: Load analysis data (site-specific from ConfigManager)
    # ══════════════════════════════════════════════════════════════════════
    
    emit("Loading analysis data...")
    
    analysis_file = paths["analysis_json"]
    
    if not analysis_file.exists():
        raise FileNotFoundError(
            f"Analysis not found for {site_id} week {week}:\n{analysis_file}\n"
            f"Run create_analysis_pipeline() first."
        )
    
    analysis = load_json(analysis_file)
    emit(f"  Analysis loaded: week {analysis.get('week')}")
    
    # ══════════════════════════════════════════════════════════════════════
    # STEP 3: Replace placeholders dynamically
    # ══════════════════════════════════════════════════════════════════════
    
    emit("Replacing placeholders...")
    
    kpi_percent = analysis.get("kpi_percent", {})
    availability_details = analysis.get("availability_details", {})
    
    # ✅ DYNAMIC: Replace with actual values from analysis
    placeholder_replacements = {
        "{{WEEK}}": str(week),
        "{{SITE_NAME}}": site_config.get("name", site_id),
        "{{LOCATION}}": site_config.get("location", ""),
        "{{OEE}}": percentage(kpi_percent.get("oee", 0)),
        "{{AVAILABILITY}}": percentage(kpi_percent.get("availability", 0)),
        "{{PERFORMANCE}}": percentage(kpi_percent.get("performance", 0)),
        "{{QUALITY}}": percentage(kpi_percent.get("quality", 0)),
        "{{PLANNED_PRODUCTION_TIME}}": minutes(availability_details.get("planned_production_time", 0)),
        "{{TOTAL_AVAILABLE_TIME}}": minutes(availability_details.get("total_available_time", 0)),
        "{{SPOUT_FAULT}}": minutes(availability_details.get("spout_fault", 0)),
        "{{MAIN_DRIVE_STOP}}": minutes(availability_details.get("main_drive_stop", 0)),
        "{{BELT_NOT_RUNNING}}": minutes(availability_details.get("belt_not_running", 0)),
        "{{REPORT_DATE}}": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    
    for placeholder, replacement in placeholder_replacements.items():
        replace_placeholder(doc, placeholder, replacement)
        print(f"  ✓ {placeholder} → {replacement}")
    
    # ══════════════════════════════════════════════════════════════════════
    # STEP 4: Insert chart images (site-specific from ConfigManager)
    # ══════════════════════════════════════════════════════════════════════
    
    emit("Inserting chart images...")
    insert_chart_images(doc, site_id, week, config_manager)
    
    # ══════════════════════════════════════════════════════════════════════
    # STEP 5: Save DOCX (site-specific path from ConfigManager)
    # ══════════════════════════════════════════════════════════════════════
    
    emit("Saving DOCX report...")
    
    docx_path = paths["report_docx"]
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    
    doc.save(str(docx_path))
    emit(f"  DOCX saved: {docx_path.name}")
    
    # ══════════════════════════════════════════════════════════════════════
    # STEP 6: Convert to PDF (site-specific path from ConfigManager)
    # ══════════════════════════════════════════════════════════════════════
    
    emit("Converting to PDF...")
    
    pdf_path = paths["report_pdf"]
    
    try:
        convert_docx_to_pdf(docx_path, pdf_path)
        emit(f"  PDF saved: {pdf_path.name}")
    
    except Exception as e:
        emit(f"  ⚠ PDF conversion failed (report available as DOCX): {e}")
        # Return DOCX path if PDF conversion fails
        return docx_path
    
    emit("Report generation complete!")
    
    return pdf_path


if __name__ == "__main__":
    # For testing only - requires ConfigManager
    # Usage: python generate_docx.py
    pass