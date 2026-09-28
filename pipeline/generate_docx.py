"""
pipeline/generate_docx.py — Multi-Site OEE Weekly Report Generator

This merges two things that used to live in separate scripts:

  1. THE MULTI-SITE PIPELINE STRUCTURE
     A ConfigManager that reads config.json and resolves, per site_id:
       - template_path, output/chart directories
       - per-week analysis.json / insights.json paths
       - the PLC -> machine-name mapping (plc_machine_map)
       - chart filenames and per-chart max widths
     Plus a single reusable entry point,
       generate_report_docx(site_id, week, config_manager, status_callback=None)
     so the SAME script/template runs for every site — only the
     config.json entry changes.

  2. THE CORRECTED DOCUMENT-GENERATION LOGIC
     Every fix that was previously only present in the per-site
     version of this script:
       - unwraps cell_info dicts (raw_value/display_value) everywhere
         instead of reading them as plain numbers/strings
       - rebuilds the executive summary / KPI-status / week-over-week
         sentences from the verified analysis.json numbers (insights.json
         has shipped with zeroed verified_kpis in the past)
       - PLC-aware downtime table (Fillpac 1 + Fillpac 2, N fault types,
         not capped at 3 rows)
       - per-shift (not per-transition) changeover table
       - new "2.2 Spout Downtime by Machine" section
       - quality table + "Quality insight" callout, with the donut chart
         swapped in
       - action-plan table driven off the verified downtime/quality data,
         built with set_row_values_by_unique_cell() so merged/gridSpan
         columns are each written exactly once
       - Section 5 (Preventive Maintenance) + its Contents-table row +
         trailing empty paragraphs removed cleanly
       - chart images resized to the template's own placeholder width
         (capped per-chart from config) instead of being force-fit
       - cross-platform DOCX -> PDF conversion (LibreOffice on
         Linux/macOS, docx2pdf on Windows, with the known
         Word.Application.Quit() AttributeError treated as non-fatal)

Public entry point:
    generate_report_docx(site_id: str, week: int, config_manager: ConfigManager,
                          status_callback=None) -> (docx_path, pdf_path)
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


# ════════════════════════════════════════════════════════════════════
# CONFIG MANAGER — multi-site config.json resolution
# ════════════════════════════════════════════════════════════════════

DEFAULT_CHART_FILENAMES = {
    "kpi_scorecard": "kpi_scorecard.png",
    "improvement_scenario": "improvement_scenario.png",
    "quality_loss_donut": "quality_loss_donut.png",
    "spout_downtime": "spout_downtime_{plc_id}.png",
}

DEFAULT_CHART_MAX_WIDTH_EMU = {
    "kpi_scorecard": 4800000,
    "improvement_scenario": 3600000,
    "spout_downtime": 5500000,
}

DEFAULT_PLC_IDS = ["PLC_01", "PLC_02"]

# Maximum number of downtime points shown in the report.
# All downtime is still used for total calculations; only the
# report-facing contributor list is capped.
MAX_DOWNTIME_POINTS = 4


class ConfigError(RuntimeError):
    pass


class ConfigManager:
    """
    Loads config.json once and resolves every site-specific value for
    a given site_id (+ week, for week-specific paths). This is what
    lets the same script/template run for every plant — only the
    config.json entry changes.

    Expected config.json shape:

    {
      "sites": {
        "jk_cement_aligarh": {
          "name": "JK Cement Aligarh",
          "location": "Aligarh, UP",
          "project_root": "/data/sites/jk_cement_aligarh",
          "template_path": "templates/Weekly_OEE_Template.docx",
          "output_dir": "output/reports",
          "chart_dir": "output/charts",
          "analysis_json": "output/analysis/week_{week}/analysis.json",
          "insights_json": "output/analysis/week_{week}/insights.json",
          "plc_machine_map": {"PLC_01": "Fillpac 1", "PLC_02": "Fillpac 2"},
          "default_plc_ids": ["PLC_01", "PLC_02"],
          "chart_filenames": {
            "kpi_scorecard": "kpi_scorecard.png",
            "improvement_scenario": "improvement_scenario.png",
            "quality_loss_donut": "quality_loss_donut.png",
            "spout_downtime": "spout_downtime_{plc_id}.png"
          },
          "chart_max_width_emu": {
            "kpi_scorecard": 4800000,
            "improvement_scenario": 3600000,
            "spout_downtime": 5500000
          }
        }
      }
    }

    analysis_json / insights_json may either contain a literal
    "{week}" placeholder (per-week subfolder layout) or point at a
    single flat file that gets overwritten weekly — both are supported.
    """

    def __init__(self, config_path):
        self.config_path = Path(config_path)

        if not self.config_path.exists():
            raise ConfigError(f"Config file not found:\n{self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as file:
            self._config = json.load(file)

        self._sites = self._config.get("sites", {})

    def _site_entry(self, site_id):
        if site_id not in self._sites:
            known = ", ".join(sorted(self._sites)) or "(none defined)"
            raise ConfigError(
                f"Unknown site_id \"{site_id}\" in {self.config_path}.\n"
                f"Known sites: {known}"
            )
        return self._sites[site_id]

    def get_site_config(self, site_id):
        """Static, non-week-specific settings for one site."""

        site = self._site_entry(site_id)
        project_root = Path(site["project_root"]).expanduser()

        template_path = site.get("template_path")

        return {
            "site_id": site_id,
            "name": site.get("name", site_id),
            "location": site.get("location", ""),
            "project_root": project_root,
            "template_path": (project_root / template_path).resolve() if template_path else None,
            "plc_machine_map": site.get("plc_machine_map", {}),
            "default_plc_ids": site.get("default_plc_ids", DEFAULT_PLC_IDS),
            "chart_filenames": {**DEFAULT_CHART_FILENAMES, **site.get("chart_filenames", {})},
            "chart_max_width_emu": {**DEFAULT_CHART_MAX_WIDTH_EMU, **site.get("chart_max_width_emu", {})},
        }

    def get_paths(self, site_id, week):
        """Week-specific file paths for one site."""

        site = self._site_entry(site_id)
        project_root = Path(site["project_root"]).expanduser()
        output_dir = (project_root / site["output_dir"]).resolve()
        chart_dir = (project_root / site.get("chart_dir", site["output_dir"])).resolve()

        def per_week(key, default):
            template = site.get(key, default)
            if "{week}" in template:
                return (project_root / template.format(week=week)).resolve()
            return (project_root / template).resolve()

        analysis_json = per_week("analysis_json", "output/analysis.json")
        insights_json = per_week("insights_json", "output/insights.json")

        machine_slug = "_".join(site.get("name", site_id).split())

        return {
            "analysis_json": analysis_json,
            "insights_json": insights_json,
            "chart_dir": chart_dir,
            "report_dir": output_dir,
            "report_docx": output_dir / f"Weekly_OEE_Report_{machine_slug}_Week_{week}.docx",
            "report_pdf": output_dir / f"Weekly_OEE_Report_{machine_slug}_Week_{week}.pdf",
        }


# ════════════════════════════════════════════════════════════════════
# GENERIC HELPERS
# ════════════════════════════════════════════════════════════════════

def load_json(file_path: Path) -> dict:
    if not file_path.exists():
        raise FileNotFoundError(f"File not found:\n{file_path}")

    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def number(value, default=0.0) -> float:
    """
    Parse a number out of whatever analysis.json handed us. Several
    cells come through as TEXT with a unit attached — "99.78%",
    "1000.0 min", "+0.19 pp" — and a bare float(value) raises
    ValueError on all of those. This strips a leading numeric token
    (sign + digits + optional decimal) off the front of the string
    and parses that.
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
    return f"{number(value):.2f}%"


def minutes(value) -> str:
    return f"{number(value):.2f} min"


def bags(value) -> str:
    return f"{number(value):,.0f}"


def change_pp(value) -> str:
    value = number(value)
    if value > 0:
        return f"+{value:.2f} pp"
    return f"{value:.2f} pp"


def direction(value) -> str:
    """Human-readable direction for a KPI delta in percentage points."""
    value = number(value)
    if value > 0.05:
        return "Improved"
    if value < -0.05:
        return "Decreased"
    return "No Change"


def raw(value, default=0.0):
    """Unwrap a cell_info dict ({'raw_value':...,'display_value':...}) to its numeric raw_value."""
    if isinstance(value, dict):
        value = value.get("raw_value")
    return number(value, default)


def disp(value, default="-"):
    """Unwrap a cell_info dict to its display_value/string."""
    if isinstance(value, dict):
        value = value.get("display_value")
    if value is None:
        return default
    return str(value)


def ordinal_suffix(day):
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def parse_report_date(value):
    """
    Parse `value` into a date. `value` may be an ISO-ish string from a
    datetime cell — including one with a clock-time and/or timezone
    label riding along, e.g. "2026-08-17 06:00:00 IST" — or a plain
    Excel display string ("8/17/2026", "17-Aug-2026", ...). Returns
    None (never raises) if nothing matches.
    """
    if not value:
        return None

    text = str(value).strip()

    text = re.sub(
        r"[T\s]+\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*[A-Za-z+\-][A-Za-z0-9+\-:]*\s*$",
        "",
        text,
    ).strip()

    candidate_formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    )

    for date_format in candidate_formats:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue

    return None


def format_report_date(raw_value, display_value):
    """
    Render a report-period date as "17th August 2026". Tries the raw
    cell value first, then the display string, then falls back to the
    display string unchanged.
    """
    parsed_date = parse_report_date(raw_value) or parse_report_date(display_value)

    if parsed_date is None:
        return display_value or "-"

    return (
        f"{parsed_date.day}{ordinal_suffix(parsed_date.day)} "
        f"{parsed_date.strftime('%B')} {parsed_date.year}"
    )


# ════════════════════════════════════════════════════════════════════
# DOCX MANIPULATION HELPERS
# ════════════════════════════════════════════════════════════════════

def clear_paragraph(paragraph):
    for run in paragraph.runs:
        run._element.getparent().remove(run._element)


def replace_paragraph_text(paragraph, text, bold=False, size=None):
    clear_paragraph(paragraph)
    run = paragraph.add_run(str(text))
    run.bold = bold
    if size:
        run.font.size = Pt(size)


def set_run_text(run, new_text):
    """
    Update a run's visible text in place while preserving its existing
    <w:br/> line breaks and formatting. Unlike replace_paragraph_text(),
    this does NOT clear the run or its siblings, so it's safe on runs
    sharing a paragraph with other content (e.g. an inline image).
    """
    t = run._r.find(qn("w:t"))

    if t is None:
        t = run._r.makeelement(qn("w:t"), {})
        run._r.append(t)

    t.text = new_text
    t.set(qn("xml:space"), "preserve")


def set_cell_text(cell, text, bold=False, size=8, align=None):
    cell.text = ""
    paragraph = cell.paragraphs[0]
    run = paragraph.add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    if align is not None:
        paragraph.alignment = align


def set_cell_text_with_bold_heading(cell, heading, body, size=8):
    """First line (heading) bold, rest (body) regular weight — for the
    'Quality insight' / 'Decision point' callout boxes."""
    cell.text = ""
    paragraph = cell.paragraphs[0]

    heading_run = paragraph.add_run(str(heading))
    heading_run.bold = True
    heading_run.font.size = Pt(size)

    if body:
        heading_run.add_break()
        body_run = paragraph.add_run(str(body))
        body_run.bold = False
        body_run.font.size = Pt(size)


def set_cell_background(cell, hex_color):
    """Set a table cell's background fill (6-digit hex, no leading '#')."""
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_color)
    cell._tc.get_or_add_tcPr().append(shading)


def set_row_values_by_unique_cell(row, values, size=7, bold=False, center_first=False):
    """
    Write `values` into a table row's LOGICAL (post-merge) columns, one
    value per logical column, regardless of how many grid cells that
    column spans (gridSpan merges make row.cells repeat the same
    underlying <w:tc> once per grid cell it spans). Writes to only the
    first grid cell seen for each unique underlying <w:tc>, consuming
    exactly one value per unique cell.
    """
    seen_tc_ids = set()
    value_iterator = iter(values)
    logical_column = 0

    for cell in row.cells:
        tc_id = id(cell._tc)

        if tc_id in seen_tc_ids:
            continue

        seen_tc_ids.add(tc_id)

        try:
            value = next(value_iterator)
        except StopIteration:
            break

        set_cell_text(
            cell,
            value,
            size=size,
            bold=bold,
            align=(WD_ALIGN_PARAGRAPH.CENTER if center_first and logical_column == 0 else None),
        )

        logical_column += 1


def clone_row(table, source_row):
    new_row = deepcopy(source_row._tr)
    table._tbl.append(new_row)
    return table.rows[-1]


def clone_row_before(table, source_row, ref_row):
    """Clone source_row's XML and insert the copy immediately before
    ref_row, instead of appending at the end of the table — used when
    a styled callout row must stay the last row while new data rows
    are inserted above it."""
    new_tr = deepcopy(source_row._tr)
    ref_row._tr.addprevious(new_tr)
    return _Row(new_tr, table)


def find_row_by_prefix(table, prefix):
    for row in table.rows:
        if row.cells[0].text.strip().startswith(prefix):
            return row
    return None


def insert_paragraph_before(reference_paragraph, text=None, bold=False, size=None):
    """Insert a brand-new paragraph immediately before reference_paragraph."""
    new_p = OxmlElement("w:p")
    reference_paragraph._p.addprevious(new_p)
    new_paragraph = Paragraph(new_p, reference_paragraph._parent)

    if text is not None:
        run = new_paragraph.add_run(str(text))
        run.bold = bold
        if size:
            run.font.size = Pt(size)

    return new_paragraph


def insert_paragraph_after(reference_element, parent, text=None, italic=False, bold=False, size=None):
    """Insert a brand-new paragraph immediately after reference_element
    (a paragraph's `._p` or a table's `._tbl`)."""
    new_p = OxmlElement("w:p")
    reference_element.addnext(new_p)
    new_paragraph = Paragraph(new_p, parent)

    if text is not None:
        run = new_paragraph.add_run(str(text))
        run.italic = italic
        run.bold = bold
        if size:
            run.font.size = Pt(size)

    return new_paragraph


def delete_rows_after(table, keep_rows):
    while len(table.rows) > keep_rows:
        table._tbl.remove(table.rows[-1]._tr)


def remove_stray_spout_sections(document):
    """
    Remove any existing "2.2 Spout Downtime by Machine" section(s) —
    heading + description + per-PLC captions/images — already sitting
    in this document. This guards against the section stacking up
    duplicates run over run (e.g. if a prior week's generated output
    ever gets reused as the next week's starting template), by
    clearing every prior copy before a fresh one is inserted.
    """
    while True:
        paragraphs_now = document.paragraphs
        start_index = None

        for index, paragraph in enumerate(paragraphs_now):
            if paragraph.text.strip().startswith("2.2 Spout Downtime by Machine"):
                start_index = index
                break

        if start_index is None:
            return

        end_index = None
        for index in range(start_index + 1, len(paragraphs_now)):
            text = paragraphs_now[index].text.strip()
            if text.startswith("3. Quality") or text.startswith("2.2 Spout Downtime by Machine"):
                end_index = index
                break

        remove_upto = end_index if end_index is not None else len(paragraphs_now)

        for index in range(start_index, remove_upto):
            element = paragraphs_now[index]._p
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)


def paragraph_has_drawing(paragraph):
    return bool(paragraph._p.findall(".//" + qn("w:drawing")))



def normalize_page_start(paragraph):
    """
    Start this section on exactly one new page.

    The template already contains manual page-break paragraphs in places.
    Setting `page_break_before=True` on the section heading as well can
    therefore create TWO consecutive page breaks, which Word/LibreOffice
    renders as an empty page.

    Remove page-break elements from the immediately preceding paragraph
    (and any preceding empty paragraphs that contain a page break), then
    use the heading's page_break_before property as the single source of
    truth for pagination.
    """
    current = paragraph._p.getprevious()

    while current is not None and current.tag == qn("w:p"):
        text = "".join(node.text or "" for node in current.iter(qn("w:t"))).strip()
        page_breaks = current.xpath('.//w:br[@w:type="page"]')

        if page_breaks:
            for br in page_breaks:
                parent = br.getparent()
                if parent is not None:
                    parent.remove(br)

        # Keep walking through empty paragraphs so a chain of manual
        # page-break paragraphs cannot produce an extra blank page.
        if text or page_breaks == []:
            break

        current = current.getprevious()

    paragraph.paragraph_format.page_break_before = True

def replace_paragraph_image(paragraph, image_path, width_emu=None, height_emu=None):
    """
    Remove a static picture from a paragraph and replace it with a
    freshly generated, data-driven chart image, keeping the original
    picture's on-page WIDTH where available. Height is deliberately
    NOT taken from the template placeholder — that box was sized for a
    different static image, so height is derived from the generated
    image's own aspect ratio to avoid stretching/squishing.
    """
    image_path = Path(image_path)

    if not image_path.exists():
        return False

    clear_paragraph(paragraph)
    run = paragraph.add_run()

    target_width_emu = width_emu or 6000000

    try:
        native = DocxImage.from_file(str(image_path))
        aspect = native.height / native.width
        run.add_picture(
            str(image_path),
            width=Emu(target_width_emu),
            height=Emu(int(target_width_emu * aspect)),
        )
    except Exception:
        run.add_picture(str(image_path), width=Emu(target_width_emu))

    return True


# ════════════════════════════════════════════════════════════════════
# PDF CONVERSION — cross-platform
# ════════════════════════════════════════════════════════════════════

def convert_docx_to_pdf(docx_path: Path, pdf_path: Path):
    """
    Convert DOCX to PDF. Windows uses docx2pdf (and treats the known
    Word.Application.Quit() AttributeError as non-fatal, since the PDF
    is usually created despite it); Linux/macOS uses LibreOffice.
    """
    if not docx_path.exists():
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")

    print("  Converting DOCX → PDF...")
    print(f"    Input: {docx_path}")
    print(f"    Output: {pdf_path}")

    if platform.system() == "Windows":
        try:
            from docx2pdf import convert
        except ImportError:
            raise ImportError("docx2pdf library not found. Install it with:\n  pip install docx2pdf")

        try:
            convert(str(docx_path), str(pdf_path))
        except AttributeError as attribute_error:
            if "Word.Application.Quit" in str(attribute_error) or "Quit" in str(attribute_error):
                if not pdf_path.exists():
                    raise
            else:
                raise
    else:
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", str(pdf_path.parent),
                str(docx_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")

        # LibreOffice names the output after the input stem; rename to
        # the exact pdf_path the caller expects if they differ.
        produced = pdf_path.parent / (docx_path.stem + ".pdf")
        if produced != pdf_path and produced.exists():
            produced.replace(pdf_path)

    if not pdf_path.exists():
        raise RuntimeError("PDF file was not created")

    print("  ✓ PDF conversion successful")


# ════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════════

def generate_report_docx(site_id: str, week: int, config_manager: ConfigManager, status_callback=None) -> tuple:
    """
    Generate the complete Weekly OEE DOCX + PDF report for one site/week.

    Steps:
      1. Resolve site config + week-specific paths via ConfigManager
      2. Load template, analysis.json, insights.json
      3. Compute every verified KPI/downtime/quality/performance value
         from analysis.json (never trust insights.json's numbers)
      4. Populate cover page, KPI dashboard, downtime, shift changeover,
         spout downtime, quality, action plan
      5. Remove the excluded Preventive Maintenance section cleanly
      6. Swap in the real generated chart images
      7. Save DOCX, convert to PDF

    Returns:
        (docx_path, pdf_path)
    """

    def emit(message):
        if status_callback:
            status_callback(message)
        print(f"[{site_id}] {message}")

    emit("Initializing report generation...")

    site_config = config_manager.get_site_config(site_id)
    paths = config_manager.get_paths(site_id, week)

    # site_config / paths may come from the project's OWN ConfigManager
    # (not the one bundled in this file), which may not expose every
    # key below. Every lookup here is defensive — .get() with a sane
    # fallback derived from whatever IS present — so a leaner config
    # manager still works; a richer one just gets its values picked
    # up instead of the fallback.

    machine_display_name = site_config.get("name", site_id)

    plc_machine_map = site_config.get("plc_machine_map", {})
    default_plc_ids = site_config.get("default_plc_ids", DEFAULT_PLC_IDS)

    chart_filenames = {**DEFAULT_CHART_FILENAMES, **(site_config.get("chart_filenames") or {})}
    chart_max_width_emu_config = {
        **DEFAULT_CHART_MAX_WIDTH_EMU,
        **(site_config.get("chart_max_width_emu") or {}),
    }

    template_path = (
        site_config.get("template_path")
        or site_config.get("template")
        or paths.get("template_path")
    )

    analysis_file = paths.get("analysis_json") or paths.get("analysis_file")
    if analysis_file is None:
        raise ConfigError(
            f"config_manager.get_paths({site_id!r}, {week!r}) did not return "
            f"'analysis_json' (or 'analysis_file'). Got keys: {sorted(paths.keys())}"
        )
    analysis_file = Path(analysis_file)

    insights_file = paths.get("insights_json") or paths.get("insights_file")
    insights_file = Path(insights_file) if insights_file else analysis_file.parent / "insights.json"

    chart_dir = paths.get("chart_dir")
    chart_dir = Path(chart_dir) if chart_dir else analysis_file.parent / "charts"

    report_dir = paths.get("report_dir") or paths.get("output_dir")
    report_dir = Path(report_dir) if report_dir else analysis_file.parent

    # ══════════════════════════════════════════════════════════════
    # LOAD TEMPLATE / DATA
    # ══════════════════════════════════════════════════════════════

    emit("Loading template...")

    template_path = Path(template_path) if template_path else None

    if not template_path or not template_path.exists():
        raise FileNotFoundError(
            f"Template not found for {site_id}:\n{template_path}\n"
            f"Check config.json template_path value."
        )

    document = Document(str(template_path))
    emit(f"  Template loaded: {template_path.name}")

    emit("Loading analysis data...")

    if not analysis_file.exists():
        raise FileNotFoundError(
            f"Analysis not found for {site_id} week {week}:\n{analysis_file}\n"
            f"Run the analysis pipeline first."
        )

    analysis = load_json(analysis_file)
    emit(f"  Analysis loaded: week {analysis.get('week')}")

    emit("Loading insights data...")

    if not insights_file.exists():
        raise FileNotFoundError(
            "Required insights.json was not found for this report:\n"
            f"{insights_file}\n\n"
            "Run the LLM insights stage first. The report generator "
            "will not use narrative from template.docx as a fallback."
        )

    insights = load_json(insights_file)

    paragraphs = document.paragraphs
    tables = document.tables

    machine = analysis.get("machine", machine_display_name)
    previous_week = analysis.get("previous_week", "-")

    report_period = analysis.get("report_period", {})

    report_start_date = format_report_date(
        report_period.get("start_raw"), report_period.get("start")
    )
    report_end_date = format_report_date(
        report_period.get("end_raw"), report_period.get("end")
    )

    # ══════════════════════════════════════════════════════════════
    # KPI PERCENTAGES
    # ══════════════════════════════════════════════════════════════

    def kpi_percent(name):
        """
        Preferred source: analysis["kpi_percent"][name] — already a
        clean 0-100 float. Fallback (older analysis.json): read
        analysis["kpi"][name]["raw_value"] and scale 0-1 -> 0-100.
        """
        value = analysis.get("kpi_percent", {}).get(name)

        if value is not None:
            return number(value)

        item = analysis.get("kpi", {}).get(name, {})
        value = raw(item, default=None)

        if value is None:
            return 0.0

        if name in ("availability", "performance", "quality") and abs(value) <= 1:
            value *= 100

        return value

    current_kpi = {
        "oee": kpi_percent("oee"),
        "availability": kpi_percent("availability"),
        "performance": kpi_percent("performance"),
        "quality": kpi_percent("quality"),
    }

    previous_kpi_raw = analysis.get("previous_kpi") or {}

    previous_kpi = {
        name: raw(previous_kpi_raw.get(name), default=None)
        for name in current_kpi
        if name in previous_kpi_raw
    }

    changes = {
        name: (current_kpi[name] - previous_kpi[name])
        for name in current_kpi
        if name in previous_kpi
    }

    oee = current_kpi["oee"]
    availability = current_kpi["availability"]
    performance = current_kpi["performance"]
    quality = current_kpi["quality"]

    KPI_TARGETS = {
        "Availability": 90.0,
        "Performance": 95.0,
        "Quality": 99.9,
        "OEE": 85.0,
    }

    def kpi_status(value, target):
        """
        Determine the scorecard status directly from the verified KPI
        value and its target.

        Status bands:
          - On Target: value >= target
          - Below Target: value is within 15 percentage points of target
          - Critical: value is more than 15 percentage points below target

        This is deterministic and does not depend on the LLM insights.
        """
        value = number(value)
        target = number(target)

        if value >= target:
            return "On Target"
        elif value >= target - 15:
            return "Below Target"
        else:
            return "Critical"

    kpi_statuses = {
        "OEE": kpi_status(oee, KPI_TARGETS["OEE"]),
        "Availability": kpi_status(availability, KPI_TARGETS["Availability"]),
        "Performance": kpi_status(performance, KPI_TARGETS["Performance"]),
        "Quality": kpi_status(quality, KPI_TARGETS["Quality"]),
    }

    # Keep the existing variable for compatibility with any later
    # code that may reference it.
    oee_status = kpi_statuses["OEE"]

    kpi_rank = [
        {"name": "Availability", "value": availability},
        {"name": "Performance", "value": performance},
        {"name": "Quality", "value": quality},
    ]

    weakest = min(kpi_rank, key=lambda item: item["value"])
    strongest = max(kpi_rank, key=lambda item: item["value"])

    # ══════════════════════════════════════════════════════════════
    # AVAILABILITY
    # ══════════════════════════════════════════════════════════════

    availability_details = analysis.get("availability_details", {})

    planned_time = raw(availability_details.get("planned_production_time_min"))
    available_time = raw(availability_details.get("total_available_time_min"))
    total_availability_loss = max(planned_time - available_time, 0)

    # ══════════════════════════════════════════════════════════════
    # DOWNTIME (PLC-aware, dynamic — any number of PLCs/fault types)
    # ══════════════════════════════════════════════════════════════

    downtime_breakdown_by_plc = analysis.get("downtime_breakdown_by_plc", {})

    fault_key_to_label = {
        "spout_fault_min": "Spout Fault",
        "main_drive_stop_min": "Main Drive Stop",
        "belt_not_running_min": "Belt Not Running",
    }

    downtime_minutes_list = []

    for plc_id in sorted(downtime_breakdown_by_plc.keys()):
        plc_entry = downtime_breakdown_by_plc[plc_id]
        machine_name = plc_entry.get("machine", plc_machine_map.get(plc_id, plc_id))

        for key, value in plc_entry.items():
            if key == "machine":
                continue

            fault_label = fault_key_to_label.get(
                key, key.replace("_min", "").replace("_", " ").title()
            )

            downtime_minutes_list.append((f"{machine_name} {fault_label}", raw(value)))

    # Backward-compatible fallback for older, single-machine analysis.json
    if not downtime_minutes_list:
        legacy_items = [
            ("Spout Fault", availability_details.get("spout_fault_min")),
            ("Main Drive Stop", availability_details.get("main_drive_stop_min")),
            ("Belt Not Running", availability_details.get("belt_not_running_min")),
        ]
        downtime_minutes_list = [(label, raw(value)) for label, value in legacy_items]

    # IMPORTANT: total downtime uses ALL recorded causes, NOT the
    # top-4 report list.
    total_downtime = (
        sum(m for _, m in downtime_minutes_list)
        or total_availability_loss
    )

    # Sort all downtime causes first, then expose ONLY the top 4
    # to the report. The full list is still retained in
    # downtime_minutes_list so total_downtime and availability
    # calculations are not artificially reduced.
    all_downtime_contributors = [
        {"reason": label, "minutes": mins}
        for label, mins in sorted(
            downtime_minutes_list,
            key=lambda item: item[1],
            reverse=True,
        )
        if mins > 0
    ]

    downtime_contributors = all_downtime_contributors[:MAX_DOWNTIME_POINTS]

    main_downtime = downtime_contributors[0] if downtime_contributors else {}

    print(
        f"  Downtime contributors for report: "
        f"TOP {MAX_DOWNTIME_POINTS} "
        f"({len(downtime_contributors)} rows)"
    )

    # ══════════════════════════════════════════════════════════════
    # QUALITY
    # ══════════════════════════════════════════════════════════════

    quality_details = analysis.get("quality_details", {})
    good_bags = raw(quality_details.get("good_bags"))

    quality_bag_items = [
        ("Out of Limit Bags", quality_details.get("out_of_limit_bags")),
        ("Burst Bags", quality_details.get("burst_bags")),
        ("Good Bags", quality_details.get("good_bags")),
    ]

    quality_bag_values = [(label, raw(value)) for label, value in quality_bag_items]

    total_bags_reported = sum(b for _, b in quality_bag_values)

    # Quality loss (for callouts) is Out of Limit + Burst — Good Bags
    # is the baseline reference, not a loss.
    quality_loss_bags = sum(b for label, b in quality_bag_values if label != "Good Bags")
    total_produced_bags = good_bags + quality_loss_bags
    quality_loss_percentage = (quality_loss_bags / total_produced_bags * 100) if total_produced_bags else 0.0

    quality_contributors = [
        {
            "reason": label,
            "bags": b,
            "percentage_of_total_bags": (b / total_bags_reported * 100) if total_bags_reported else 0.0,
        }
        for label, b in sorted(quality_bag_values, key=lambda item: item[1], reverse=True)
        if b > 0
    ]

    quality_loss_only = [item for item in quality_contributors if item["reason"] != "Good Bags"]
    main_quality = quality_loss_only[0] if quality_loss_only else {}

    # ══════════════════════════════════════════════════════════════
    # PERFORMANCE
    # ══════════════════════════════════════════════════════════════

    performance_details = analysis.get("performance_details", {})
    actual_good_bags = raw(performance_details.get("actual_good_bags"))
    expected_good_bags = raw(performance_details.get("expected_good_bags"))
    production_gap = max(expected_good_bags - actual_good_bags, 0)

    # ══════════════════════════════════════════════════════════════
    # MACHINE PARAMETERS
    # ══════════════════════════════════════════════════════════════

    machine_parameters = analysis.get("machine_parameters", {})
    rpm = disp(machine_parameters.get("rpm"))
    bags_per_hour = disp(machine_parameters.get("bags_per_hour"))
    bags_per_minute = disp(machine_parameters.get("bags_per_minute"))

    # ══════════════════════════════════════════════════════════════
    # SHIFT CHANGEOVER / SPOUT DOWNTIME (raw dicts, consumed below)
    # ══════════════════════════════════════════════════════════════

    shift_changeover = analysis.get("shift_changeover", {})
    spout_downtime = analysis.get("spout_downtime", {})

    # ══════════════════════════════════════════════════════════════
    # AI INSIGHTS
    #
    # IMPORTANT:
    # Narrative comes ONLY from insights.json.
    #
    # analysis.json = numerical source of truth
    # insights.json = LLM narrative source of truth
    # template.docx = layout only
    #
    # There is intentionally NO deterministic narrative fallback.
    # If the LLM fields are missing, report generation stops.
    # ══════════════════════════════════════════════════════════════

    _insights_ai = insights.get("ai_insights", {}) or {}

    def ai_text(key):
        """
        Read an LLM-generated narrative field directly from insights.json.
        """

        value = _insights_ai.get(key)

        if value is None:
            value = insights.get(key)

        if value is None:
            return ""

        return str(value).strip()

    required_ai_fields = [
        "executive_summary",
        "main_loss_driver",
        "performance_concern",
        "strongest_kpi",
        "week_over_week_trend",
        "quality_summary",
        "main_quality_issue",
        "key_decision_point",
    ]

    missing_ai_fields = [
        field
        for field in required_ai_fields
        if not ai_text(field)
    ]

    if missing_ai_fields:
        raise RuntimeError(
            "The insights.json used for this report does not contain "
            "all required LLM-generated narrative fields.\n"
            f"Missing: {', '.join(missing_ai_fields)}\n"
            f"File: {insights_file}\n\n"
            "Run the updated local_llm_client.py first so that a fresh "
            "insights.json is generated by the local LLM."
        )

    executive_summary = ai_text("executive_summary")
    main_loss_driver_text = ai_text("main_loss_driver")
    performance_concern_text = ai_text("performance_concern")
    strongest_kpi_text = ai_text("strongest_kpi")
    week_over_week_trend_text = ai_text("week_over_week_trend")
    quality_summary_text = ai_text("quality_summary")
    main_quality_text = ai_text("main_quality_issue")
    decision_body = ai_text("key_decision_point")

    # Never allow the template's own placeholder prose to be treated
    # as a successful AI result. This catches cases where a stale
    # insights.json was accidentally populated with template text.
    template_placeholder_markers = (
        "insert summary here",
        "write summary here",
        "add summary here",
        "enter summary here",
        "sample summary",
        "placeholder",
    )

    for field_name in required_ai_fields:
        value = ai_text(field_name).lower()

        if any(marker in value for marker in template_placeholder_markers):
            raise RuntimeError(
                f"AI field '{field_name}' in insights.json appears to "
                "contain template placeholder text. Report generation stopped.\n"
                f"File: {insights_file}"
            )

    # ══════════════════════════════════════════════════════════════
    # PAGE 1 — COVER
    # ══════════════════════════════════════════════════════════════

    emit("Filling cover page...")

    # ══════════════════════════════════════════════════════════════

    emit("Filling cover page...")

    # The cover block is a single paragraph in the template.
    #
    # Template run layout:
    #   [0] = initial newline
    #   [1] = title
    #   [2] = newline
    #   [3] = subtitle
    #   [4] = newline
    #   [5] = report week
    #   [6] = newline
    #   [7] = start/end date
    #
    # IMPORTANT:
    # The title must be written to run [1]. Writing to run [0] leaves
    # the original template title behind, which causes text such as
    # "Weekly OEE Insight Report - Fillpac 1 & 2 (Packer 1 + Packer 2)"
    # to appear beside the new title.

    cover_paragraph = paragraphs[0]
    cover_runs = cover_paragraph.runs

    if len(cover_runs) >= 8:
        # Exact cover title requested.
        set_run_text(cover_runs[1], "Weekly OEE Report")

        set_run_text(
            cover_runs[3],
            "Weekly executive summary with quantified insights, loss drivers and action priorities",
        )

        set_run_text(cover_runs[5], f"Report Week: Week {week}")

        set_run_text(
            cover_runs[7],
            f"Start Date: {report_start_date}     End Date: {report_end_date}",
        )

        report_week_run = cover_runs[5]

    # ══════════════════════════════════════════════════════════════
    # PAGE 2 — EXECUTIVE DASHBOARD
    # ══════════════════════════════════════════════════════════════

    emit("Filling executive dashboard...")

    matched_summary = False
    summary_heading_seen = False

    for paragraph in paragraphs:
        text = paragraph.text.strip()

        if text == "1. Executive Insight Dashboard":
            replace_paragraph_text(paragraph, "1. Executive Insight Dashboard", bold=True, size=14)
            normalize_page_start(paragraph)

        elif text.startswith("This page explains the numbers"):
            replace_paragraph_text(
                paragraph,
                "This page explains the verified OEE results in plain language for operations, maintenance and management users.",
                size=9,
            )

        elif text == "Summary":
            replace_paragraph_text(paragraph, "Summary", bold=True, size=10)
            summary_heading_seen = True

        elif summary_heading_seen and text:
            replace_paragraph_text(paragraph, executive_summary, size=8)
            matched_summary = True
            summary_heading_seen = False

    if not matched_summary:
        raise RuntimeError(
            "Required AI narrative placeholder 'Summary' was not found "
            "in the template. Report generation stopped so template "
            "narrative cannot silently remain in the report."
        )

    # KPI table

    kpi_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "KPI" in full_text and "Current" in full_text and "Industry standard" in full_text:
            kpi_table = table
            break

    if not kpi_table:
        emit(
            "  ⚠ WARNING: KPI Scorecard table not found in template — "
            "KPI table left unfilled/unchanged."
        )

    if kpi_table:
        kpi_body_template = kpi_table.rows[1] if len(kpi_table.rows) > 1 else kpi_table.rows[0]
        delete_rows_after(kpi_table, 2)

        kpi_rows = [
            ("OEE", percentage(oee), "85% world-class", kpi_statuses["OEE"]),
            ("Availability", percentage(availability), "90%+", kpi_statuses["Availability"]),
            ("Performance", percentage(performance), "95%+", kpi_statuses["Performance"]),
            ("Quality", percentage(quality), "99.9%", kpi_statuses["Quality"]),
        ]

        for row_data in kpi_rows:
            row = clone_row(kpi_table, kpi_body_template)
            for index, value in enumerate(row_data):
                set_cell_text(row.cells[index], value, size=7, align=WD_ALIGN_PARAGRAPH.CENTER)

        kpi_table._tbl.remove(kpi_body_template._tr)

    # Top insight boxes

    insight_box_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "Main loss driver" in full_text and ("Performance" in full_text or "Quality" in full_text):
            insight_box_table = table
            break

    if not insight_box_table:
        raise RuntimeError(
            "Required AI insight-box table was not found in the template. "
            "Report generation stopped so stale template narrative cannot "
            "remain in the report."
        )

    if insight_box_table:
        row = insight_box_table.rows[0]

        # The headings are fixed report structure. The complete body text
        # is copied from insights.json and is never reconstructed from the
        # template or analysis.json.
        set_cell_text_with_bold_heading(
            row.cells[0],
            "1. Main loss driver",
            main_loss_driver_text,
            size=7,
        )

        set_cell_text_with_bold_heading(
            row.cells[1],
            "2. Performance concern",
            performance_concern_text,
            size=7,
        )

        set_cell_text_with_bold_heading(
            row.cells[2],
            "3. Strongest KPI",
            strongest_kpi_text,
            size=7,
        )

    # Week comparison table

    comparison_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if ("KPI" in full_text and "Management insight" in full_text) or (
            "Previous" in full_text and "Current" in full_text and "Change" in full_text
        ):
            comparison_table = table
            break

    if not comparison_table:
        emit(
            "  ⚠ WARNING: week-over-week comparison table not found in template — "
            "left unfilled/unchanged."
        )

    for paragraph in paragraphs:
        if paragraph.text.strip().endswith("Comparison") and "vs Week" in paragraph.text:
            replace_paragraph_text(paragraph, f"Week {previous_week} vs Week {week} Comparison", bold=True, size=10)

    if comparison_table:
        header_row = comparison_table.rows[0]

        # Positional, not text-matched: the header cells hold whatever
        # the PREVIOUS run wrote there (e.g. "Week 35"/"Week 36"), so
        # matching literal "Week 33"/"Week 34" text only ever worked on
        # a pristine, never-before-run template. Column 1 is always the
        # previous-week header, column 2 always the current-week one.
        if len(header_row.cells) > 2:
            set_cell_text(header_row.cells[1], f"Week {previous_week}", bold=True, size=7)
            set_cell_text(header_row.cells[2], f"Week {week}", bold=True, size=7)

        comparison_body_template = comparison_table.rows[1] if len(comparison_table.rows) > 1 else comparison_table.rows[0]
        delete_rows_after(comparison_table, 2)

        def previous_cell(name):
            return percentage(previous_kpi[name]) if name in previous_kpi else "-"

        def change_cell(name):
            return change_pp(changes[name]) if name in changes else "-"

        management_insight = week_over_week_trend_text

        comparison_rows = [
            ("OEE", previous_cell("oee"), percentage(oee), change_cell("oee"), management_insight),
            ("Availability", previous_cell("availability"), percentage(availability), change_cell("availability"), management_insight),
            ("Performance", previous_cell("performance"), percentage(performance), change_cell("performance"), management_insight),
            ("Quality", previous_cell("quality"), percentage(quality), change_cell("quality"), management_insight),
        ]

        for row_data in comparison_rows:
            row = clone_row(comparison_table, comparison_body_template)
            for index, value in enumerate(row_data):
                set_cell_text(row.cells[index], value, size=7, align=(WD_ALIGN_PARAGRAPH.CENTER if index == 0 else None))

        comparison_table._tbl.remove(comparison_body_template._tr)

        insert_paragraph_after(
            comparison_table._tbl,
            paragraphs[0]._parent,
            "pp = percentage points, the straight difference between two percentages (e.g. 45% to 50% is +5 pp), not a percent change.",
            italic=True,
            size=7,
        )

    # Machine parameters

    for paragraph in paragraphs:
        if paragraph.text.strip() == "Machine Parameters":
            replace_paragraph_text(paragraph, "Machine Parameters", bold=True, size=10)

    machine_parameter_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "RPM" in full_text and "Bags per Hour" in full_text and "Bags per Minute" in full_text:
            machine_parameter_table = table
            break

    if machine_parameter_table:
        for row in machine_parameter_table.rows:
            first_cell = row.cells[0].text.strip()
            if first_cell == "RPM":
                set_cell_text(row.cells[1], rpm)
            elif first_cell == "Bags per Hour":
                set_cell_text(row.cells[1], bags_per_hour)
            elif first_cell == "Bags per Minute":
                set_cell_text(row.cells[1], bags_per_minute)

    # Keep the complete executive dashboard on one physical page.
    # The template is designed for a one-page dashboard; Word may otherwise
    # push the final comparison rows onto a new page when long management
    # insight text wraps. Compact only the dashboard content.
    for table in [kpi_table, insight_box_table, comparison_table]:
        if table is not None:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.space_before = Pt(0)
                        paragraph.paragraph_format.space_after = Pt(0)
                        paragraph.paragraph_format.line_spacing = 0.9

    # Also remove extra spacing around dashboard paragraphs.
    for paragraph in paragraphs:
        txt = paragraph.text.strip()
        if (
            txt == "1. Executive Insight Dashboard"
            or txt == "Summary"
            or txt.startswith("This page explains the verified OEE results")
            or txt.endswith("Comparison")
            or txt.startswith("pp = percentage points")
        ):
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.line_spacing = 0.9

    # ══════════════════════════════════════════════════════════════
    # PAGE 3 — DOWNTIME
    # ══════════════════════════════════════════════════════════════

    emit("Filling downtime section...")

    downtime_heading_seen = False
    for paragraph in paragraphs:
        text = paragraph.text.strip()
        if text == "2. Why OEE Is Going Down":
            replace_paragraph_text(paragraph, "2. Why OEE Is Going Down", bold=True, size=15)
            normalize_page_start(paragraph)
            downtime_heading_seen = True
        elif downtime_heading_seen and text:
            replace_paragraph_text(paragraph, main_loss_driver_text, size=9)
            downtime_heading_seen = False

    downtime_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "Fault code" in full_text and "Immediate action" in full_text:
            downtime_table = table
            break

    if not downtime_table:
        raise RuntimeError(
            "Required downtime table was not found in the template. "
            "Report generation stopped."
        )

    if downtime_table:
        downtime_peach_template = downtime_table.rows[1] if len(downtime_table.rows) > 1 else downtime_table.rows[0]
        downtime_yellow_template = downtime_table.rows[2] if len(downtime_table.rows) > 2 else downtime_peach_template

        decision_row = find_row_by_prefix(downtime_table, "Decision point")

        if decision_row is None:
            raise RuntimeError(
                "Required 'Decision point' row was not found in the downtime table. "
                "Report generation stopped so template decision text cannot remain."
            )

        peach_tr_copy = deepcopy(downtime_peach_template._tr)
        yellow_tr_copy = deepcopy(downtime_yellow_template._tr)

        for row in list(downtime_table.rows)[1:]:
            if decision_row is not None and row._tr is decision_row._tr:
                break
            downtime_table._tbl.remove(row._tr)

        # downtime_contributors is already capped at TOP 4 above.
        # Therefore this table can never contain more than 4 downtime rows.
        for rank, item in enumerate(downtime_contributors, start=1):
            reason = item.get("reason", "Unknown")
            loss_minutes = number(item.get("minutes"))

            insight = f"{reason} accounts for {loss_minutes:.2f} minutes of recorded downtime."
            action = f"Prioritize investigation of {reason} using available production and equipment records."

            source_tr = peach_tr_copy if rank == 1 else yellow_tr_copy

            if decision_row is not None:
                new_tr = deepcopy(source_tr)
                decision_row._tr.addprevious(new_tr)
                row = _Row(new_tr, downtime_table)
            else:
                new_tr = deepcopy(source_tr)
                downtime_table._tbl.append(new_tr)
                row = _Row(new_tr, downtime_table)

            values = [str(rank), reason, f"{loss_minutes:.2f} min", insight, action]

            for index, value in enumerate(values):
                set_cell_text(row.cells[index], value, size=7)

        if decision_row is not None:
            decision_text = f"Decision point\n{decision_body}"

            set_cell_text(decision_row.cells[0], decision_text, size=8)

    # ══════════════════════════════════════════════════════════════
    # SHIFT CHANGEOVER (per-shift, not per-transition)
    # ══════════════════════════════════════════════════════════════

    emit("Filling shift changeover section...")

    for paragraph in paragraphs:
        if "Shift Changeover Delay Time" in paragraph.text:
            replace_paragraph_text(paragraph, f"2.1 Shift Changeover Delay Time - Week {week}", bold=True, size=11)
        elif paragraph.text.strip().startswith("Shift changeover delay is"):
            replace_paragraph_text(
                paragraph,
                f"Shift changeover delay is a controllable availability loss. Week {week} data show delay time during handover and start-up stabilization.",
                size=9,
            )

    changeover_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if (
            "A to B shift" in full_text
            or "B to C shift" in full_text
            or "C to A shift" in full_text
            or "Shift A" in full_text
            or "Shift B" in full_text
            or "Shift C" in full_text
        ):
            changeover_table = table
            break

    SHIFT_ORDER = ["A", "B", "C"]

    if changeover_table:
        header_row = changeover_table.rows[0]
        total_row = find_row_by_prefix(changeover_table, "Total")

        data_rows = [
            row
            for row in changeover_table.rows
            if row._tr is not header_row._tr and (total_row is None or row._tr is not total_row._tr)
        ]

        while len(data_rows) < len(SHIFT_ORDER):
            template_row = data_rows[-1] if data_rows else header_row
            new_tr = deepcopy(template_row._tr)

            if total_row is not None:
                total_row._tr.addprevious(new_tr)
            else:
                changeover_table._tbl.append(new_tr)

            data_rows.append(_Row(new_tr, changeover_table))

        while len(data_rows) > len(SHIFT_ORDER):
            extra_row = data_rows.pop()
            changeover_table._tbl.remove(extra_row._tr)

        total_delay = 0.0

        for shift_letter, row in zip(SHIFT_ORDER, data_rows):
            value = raw(
                shift_changeover.get(
                    shift_letter,
                    shift_changeover.get(
                        f"shift_{shift_letter.lower()}_cumulative_delay",
                        shift_changeover.get(
                            f"shift_{shift_letter.lower()}_delay",
                            0,
                        ),
                    ),
                )
            )
            total_delay += value

            set_cell_text(row.cells[0], f"Shift {shift_letter} Changeover Delay")
            set_cell_text(row.cells[1], f"{value:.2f} min")

        if total_row is not None:
            set_cell_text(total_row.cells[0], f"Total Week {week} delay")
            set_cell_text(total_row.cells[1], f"{total_delay:.2f} min")

    # ══════════════════════════════════════════════════════════════
    # 2.2 SPOUT DOWNTIME BY MACHINE (inserted above "3. Quality")
    # ══════════════════════════════════════════════════════════════

    emit("Inserting spout downtime section...")

    # Clear out any stale copy of this section left over from a
    # previous run before inserting a fresh one — this is what was
    # producing the duplicate "2.2" page.
    remove_stray_spout_sections(document)

    quality_heading_paragraph = None

    for paragraph in document.paragraphs:
        if paragraph.text.strip().startswith("3. Quality"):
            quality_heading_paragraph = paragraph
            break

    if quality_heading_paragraph is not None:
        insert_paragraph_before(quality_heading_paragraph, f"2.2 Spout Downtime by Machine - Week {week}", bold=True, size=11)

        insert_paragraph_before(
            quality_heading_paragraph,
            "Spout-level downtime highlights which individual spouts are driving each machine's Spout Fault minutes, so maintenance can target the specific spouts responsible rather than the machine as a whole.",
            size=9,
        )

        spout_plc_ids = sorted(spout_downtime.keys()) or list(default_plc_ids)

        for plc_id in spout_plc_ids:
            plc_entry = spout_downtime.get(plc_id, {})

            # Prefer the machine name analysis.json already carries per
            # PLC (downtime_breakdown_by_plc[plc_id]["machine"]) — that's
            # the reliable source. Only fall back to the config's
            # plc_machine_map, then the bare plc_id, if it's missing.
            machine_name = downtime_breakdown_by_plc.get(plc_id, {}).get("machine")

            if not machine_name:
                if isinstance(plc_entry, list):
                    machine_name = plc_machine_map.get(plc_id, plc_id)
                else:
                    machine_name = plc_entry.get("machine", plc_id)

            insert_paragraph_before(quality_heading_paragraph, f"{machine_name} ({plc_id})", bold=True, size=9)

            image_paragraph = insert_paragraph_before(quality_heading_paragraph)

            spout_chart_name = chart_filenames["spout_downtime"].format(plc_id=plc_id.lower())

            replace_paragraph_image(
                image_paragraph,
                chart_dir / spout_chart_name,
                width_emu=chart_max_width_emu_config.get("spout_downtime", 5500000),
            )

    # Body may have changed via remove_stray_spout_sections /
    # insert_paragraph_before above — re-fetch the live paragraph list
    # before continuing so later sections walk current content.
    paragraphs = document.paragraphs

    # ══════════════════════════════════════════════════════════════
    # PAGE 4 — QUALITY
    # ══════════════════════════════════════════════════════════════

    emit("Filling quality section...")

    matched_quality_intro = False
    quality_heading_seen = False

    for paragraph in paragraphs:
        text = paragraph.text.strip()

        if text.startswith("3. Quality"):
            replace_paragraph_text(paragraph, "3. Quality, Fault Risk and Improvement Opportunity", bold=True, size=15)
            normalize_page_start(paragraph)
            quality_heading_seen = True

        elif quality_heading_seen and text:
            replace_paragraph_text(paragraph, quality_summary_text, size=9)
            matched_quality_intro = True
            quality_heading_seen = False

    if not matched_quality_intro:
        raise RuntimeError(
            "Required AI quality-summary placeholder was not found in the template. "
            "Report generation stopped so stale template quality narrative cannot remain."
        )

    quality_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "Quality Loss Mix" in full_text or "Sample loss" in full_text or "Control action" in full_text:
            quality_table = table
            break

    if not quality_table:
        raise RuntimeError(
            "Required Quality Loss Mix table was not found in the template. "
            "Report generation stopped."
        )

    if quality_table:
        for cell in quality_table.rows[2].cells:
            if cell.text.strip() == "Sample loss":
                set_cell_text(cell, "Quantity", bold=True, size=8)

        header_rows_to_keep = min(3, len(quality_table.rows))

        quality_body_template = (
            quality_table.rows[header_rows_to_keep]
            if len(quality_table.rows) > header_rows_to_keep
            else quality_table.rows[-1]
        )

        quality_insight_row = find_row_by_prefix(quality_table, "Quality insight")

        if quality_insight_row is None:
            raise RuntimeError(
                "Required 'Quality insight' row was not found in the quality table. "
                "Report generation stopped so stale template narrative cannot remain."
            )

        quality_body_tr_copy = deepcopy(quality_body_template._tr)

        for row in list(quality_table.rows)[header_rows_to_keep:]:
            if quality_insight_row is not None and row._tr is quality_insight_row._tr:
                break
            quality_table._tbl.remove(row._tr)

        for code_number, item in enumerate(quality_contributors[:5], start=1):
            reason = item.get("reason", "Unknown")
            loss_bags = number(item.get("bags"))
            share = number(item.get("percentage_of_total_bags"))
            is_good = reason == "Good Bags"

            meaning = f"{reason} represents {share:.2f}% of total bags produced this week."
            control_action = (
                "Baseline reference — no corrective action required."
                if is_good
                else f"Review process records associated with {reason} and prioritize corrective action."
            )

            new_tr = deepcopy(quality_body_tr_copy)

            if quality_insight_row is not None:
                quality_insight_row._tr.addprevious(new_tr)
            else:
                quality_table._tbl.append(new_tr)

            row = _Row(new_tr, quality_table)

            values = [str(code_number), reason, f"{loss_bags:.0f} bags", f"{loss_bags:.0f} bags", meaning, control_action]

            for index, value in enumerate(values):
                if index < len(row.cells):
                    set_cell_text(
                        row.cells[index], value, size=7, bold=(index == 0),
                        align=(WD_ALIGN_PARAGRAPH.CENTER if index == 0 else None),
                    )

        if len(quality_table.rows) > 1:
            donut_paragraph = quality_table.rows[1].cells[0].paragraphs[0]

            if paragraph_has_drawing(donut_paragraph):
                drawing = donut_paragraph._p.find(".//" + qn("w:drawing"))
                width_emu = None
                height_emu = None

                if drawing is not None:
                    extent = drawing.find(".//" + qn("wp:extent"))
                    if extent is not None:
                        width_emu = int(extent.get("cx"))
                        height_emu = int(extent.get("cy"))

                replace_paragraph_image(
                    donut_paragraph, chart_dir / chart_filenames["quality_loss_donut"], width_emu, height_emu
                )

        if quality_insight_row is not None:
            set_cell_text_with_bold_heading(
                quality_insight_row.cells[0],
                "Quality insight",
                main_quality_text,
                size=8,
            )

    # ══════════════════════════════════════════════════════════════
    # PAGE 5 — ACTION PLAN
    # ══════════════════════════════════════════════════════════════

    emit("Filling action plan...")

    for paragraph in paragraphs:
        if paragraph.text.strip().startswith("4. Action Plan"):
            replace_paragraph_text(paragraph, "4. Action Plan: What to Do Next Week", bold=True, size=15)
            normalize_page_start(paragraph)
        elif paragraph.text.strip().startswith("Actions are prioritized"):
            replace_paragraph_text(
                paragraph,
                "Actions are prioritized from the verified OEE losses and AI-generated recommendations.",
                size=9,
            )

    action_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "Priority" in full_text and "Success measure" in full_text and "Review frequency" in full_text:
            action_table = table
            break

    if action_table:
        action_peach_template = action_table.rows[1] if len(action_table.rows) > 1 else action_table.rows[0]
        action_yellow_template = action_table.rows[4] if len(action_table.rows) > 4 else action_peach_template

        expected_header_row = find_row_by_prefix(action_table, "If action is completed")

        peach_tr_copy = deepcopy(action_peach_template._tr)
        yellow_tr_copy = deepcopy(action_yellow_template._tr)

        for row in list(action_table.rows)[1:]:
            if expected_header_row is not None and row._tr is expected_header_row._tr:
                break
            action_table._tbl.remove(row._tr)

        # Built directly from this week's verified downtime + quality
        # data (the two levers that actually move OEE), not the
        # free-form AI "recommended_actions" text.
        action_candidates = []

        for item in downtime_contributors:
            action_candidates.append({
                "priority": "P1",
                "reason": item.get("reason", "Downtime loss"),
                "action": (
                    f"Reduce {item.get('reason')} downtime "
                    f"(currently {number(item.get('minutes')):.2f} minutes this week) to recover Availability."
                ),
                "success_measure": (
                    f"Cut {item.get('reason')} downtime versus this week's {number(item.get('minutes')):.2f} minutes."
                ),
                "review_frequency": "Daily",
            })

        for item in quality_loss_only:
            action_candidates.append({
                "priority": "P2",
                "reason": item.get("reason", "Quality loss"),
                "action": (
                    f"Reduce {item.get('reason')} (currently {number(item.get('bags')):.0f} bags this week) "
                    f"to recover Quality yield."
                ),
                "success_measure": (
                    f"Cut {item.get('reason')} bags versus this week's {number(item.get('bags')):.0f} bags."
                ),
                "review_frequency": "Weekly",
            })

        for candidate in action_candidates[:4]:
            priority = candidate["priority"]
            fault_code = candidate["reason"]
            owner = "Maintenance + Production" if priority == "P1" else "Production"
            success_measure = candidate["success_measure"]
            review_frequency = candidate["review_frequency"]
            action = candidate["action"]

            source_tr = peach_tr_copy if priority == "P1" else yellow_tr_copy

            if expected_header_row is not None:
                new_tr = deepcopy(source_tr)
                expected_header_row._tr.addprevious(new_tr)
                row = _Row(new_tr, action_table)
            else:
                new_tr = deepcopy(source_tr)
                action_table._tbl.append(new_tr)
                row = _Row(new_tr, action_table)

            values = [priority, owner, owner, fault_code, action, success_measure, success_measure, review_frequency]

            set_row_values_by_unique_cell(row, values, size=7, center_first=True)

    # Expected improvement block

    if action_table:
        rows_now = list(action_table.rows)
        header_index = None

        for position, row in enumerate(rows_now):
            if row.cells[0].text.strip() == "If action is completed":
                header_index = position
                break

        if header_index is not None:
            gap_to_target = max(85 - oee, 0)

            top1 = downtime_contributors[0] if len(downtime_contributors) > 0 else None
            top2 = downtime_contributors[1] if len(downtime_contributors) > 1 else None

            step1_oee = oee + gap_to_target * 0.45
            step2_oee = oee + gap_to_target * 0.75
            step3_oee = min(oee + gap_to_target, max(oee, 85))

            top_quality = quality_loss_only[0] if quality_loss_only else None

            scenario_rows = [
                (
                    f"{top1.get('reason')} reduced" if top1 else "Top loss reduced",
                    "Availability improves first",
                    f"~{step1_oee:.2f}%",
                    "Continue the countermeasure until stable",
                ),
                (
                    f"{top1.get('reason')} plus {top2.get('reason')} addressed" if top1 and top2 else "Top losses addressed",
                    "Near-target recovery",
                    f"~{step2_oee:.2f}%",
                    (
                        f"Shift focus to {top_quality.get('reason')}, the next-largest quality contributor"
                        if top_quality
                        else "Shift focus to the next-largest quality contributor"
                    ),
                ),
                (
                    f"Availability stable + {top_quality.get('reason')} reduced" if top_quality else "Availability stable + quality improved",
                    "Target recovery",
                    f"~{step3_oee:.2f}%",
                    "Standardize the new checks into SOP",
                ),
            ]

            for offset, values in enumerate(scenario_rows, start=1):
                row_position = header_index + offset
                if row_position < len(rows_now):
                    set_row_values_by_unique_cell(rows_now[row_position], values, size=7)

    # ══════════════════════════════════════════════════════════════
    # PAGE 6 — PREVENTIVE MAINTENANCE (excluded from this report)
    # ══════════════════════════════════════════════════════════════

    emit("Removing excluded section (Preventive Maintenance)...")

    section5_heading = None
    for paragraph in paragraphs:
        if paragraph.text.strip().startswith("5. Preventive Maintenance"):
            section5_heading = paragraph
            break

    section5_intro = None
    for paragraph in paragraphs:
        if paragraph.text.strip().startswith("Preventive maintenance tasks"):
            section5_intro = paragraph
            break

    pm_table = None
    technical_table = None

    for table in tables:
        full_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "PM ID" in full_text and "Preventive maintenance activity" in full_text:
            pm_table = table
        elif "Visit ID" in full_text and "Target" in full_text:
            technical_table = table

    for element_paragraph in (section5_heading, section5_intro):
        if element_paragraph is not None:
            element = element_paragraph._p
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)

    for element_table in (pm_table, technical_table):
        if element_table is not None:
            element = element_table._tbl
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)

    # Orphaned "Technical Visit Required" sub-heading left behind by
    # the removal above.

    technical_visit_heading = None
    for paragraph in paragraphs:
        if paragraph.text.strip() == "Technical Visit Required":
            technical_visit_heading = paragraph
            break

    if technical_visit_heading is not None:
        element = technical_visit_heading._p
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)

    # Contents table — drop the section 5 row

    contents_table = None

    for table in tables:
        header_text = " ".join(cell.text.strip() for cell in table.rows[0].cells)
        if "Section" in header_text and "Main heading" in header_text and "Page" in header_text:
            contents_table = table
            break

    if contents_table is not None:
        section5_row = None
        for row in contents_table.rows:
            row_text = " ".join(cell.text.strip() for cell in row.cells)
            if "Preventive Maintenance" in row_text:
                section5_row = row
                break

        if section5_row is not None:
            element = section5_row._tr
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)

    # Trailing empty paragraphs left over from section 5's spacing —
    # strip back to the last paragraph with real content, without
    # touching the section properties.

    body = document.element.body
    body_children = list(body)
    sect_pr_tag = qn("w:sectPr")

    index = len(body_children) - 1

    while index >= 0:
        child = body_children[index]

        if child.tag == sect_pr_tag:
            index -= 1
            continue

        if child.tag != qn("w:p"):
            break

        has_drawing = child.find(".//" + qn("w:drawing")) is not None
        text = "".join(node.text or "" for node in child.iter(qn("w:t"))).strip()

        if has_drawing or text:
            break

        body.remove(child)
        index -= 1

    # ══════════════════════════════════════════════════════════════
    # REPLACE STATIC CHART IMAGES WITH REAL, DATA-DRIVEN CHARTS
    # ══════════════════════════════════════════════════════════════

    emit("Inserting chart images...")

    chart_paragraphs = [paragraph for paragraph in paragraphs if paragraph_has_drawing(paragraph)]

    # The cover photo paragraph (paragraphs[2]) is deliberately left
    # untouched by the cover-page fix above; skip it here too.
    data_chart_paragraphs = [paragraph for paragraph in chart_paragraphs if paragraph is not paragraphs[2]]

    chart_keys_in_order = ["kpi_scorecard", "improvement_scenario"]

    chart_image_sequence = [chart_dir / chart_filenames[key] for key in chart_keys_in_order]

    max_chart_width_emu = {
        chart_dir / chart_filenames[key]: chart_max_width_emu_config.get(key)
        for key in chart_keys_in_order
        if chart_max_width_emu_config.get(key)
    }

    for paragraph, chart_path in zip(data_chart_paragraphs, chart_image_sequence):
        drawing = paragraph._p.find(".//" + qn("w:drawing"))
        width_emu = None
        height_emu = None

        if drawing is not None:
            extent = drawing.find(".//" + qn("wp:extent"))
            if extent is not None:
                width_emu = int(extent.get("cx"))
                height_emu = int(extent.get("cy"))

        max_width_emu = max_chart_width_emu.get(chart_path)

        if max_width_emu and width_emu and width_emu > max_width_emu:
            # Preserve aspect ratio when the configured maximum width applies.
            if height_emu:
                height_emu = int(height_emu * max_width_emu / width_emu)
            width_emu = max_width_emu

        # The KPI scorecard is the largest vertical element on the dashboard.
        # Trim its height slightly so the full comparison table stays on page 2.
        if chart_path.name == chart_filenames.get("kpi_scorecard") and height_emu:
            height_emu = int(height_emu * 0.82)

        replace_paragraph_image(paragraph, chart_path, width_emu, height_emu)

    if not chart_dir.exists():
        emit(f"  ⚠ Chart directory not found: {chart_dir}")

    # ══════════════════════════════════════════════════════════════
    # FOOTER METADATA
    # ══════════════════════════════════════════════════════════════

    for section in document.sections:
        footer = section.footer

        if footer.paragraphs:
            paragraph = footer.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            clear_paragraph(paragraph)

            run = paragraph.add_run(f"{machine} | Week {week} | Previous Week {previous_week}")
            run.font.size = Pt(7)

    # ══════════════════════════════════════════════════════════════
    # SAVE DOCX / CONVERT TO PDF
    # ══════════════════════════════════════════════════════════════

    emit("Saving DOCX report...")

    machine_slug = "_".join(str(machine).replace("/", " ").replace("\\", " ").split())

    docx_path = paths.get("report_docx") or paths.get("docx_path")
    docx_path = Path(docx_path) if docx_path else report_dir / f"Weekly_OEE_Report_{machine_slug}_Week_{week}.docx"

    pdf_path = paths.get("report_pdf") or paths.get("pdf_path")
    pdf_path = Path(pdf_path) if pdf_path else report_dir / f"Weekly_OEE_Report_{machine_slug}_Week_{week}.pdf"

    report_dir.mkdir(parents=True, exist_ok=True)
    document.save(str(docx_path))
    emit(f"  DOCX saved: {docx_path.name}")

    emit("Converting to PDF...")

    try:
        convert_docx_to_pdf(docx_path, pdf_path)
        emit(f"  PDF saved: {pdf_path.name}")
        emit("Report generation complete!")
        return docx_path, pdf_path
    except Exception as e:
        emit(f"  ⚠ PDF conversion failed (report available as DOCX): {e}")
        emit("Report generation complete (DOCX only)!")
        return docx_path, docx_path


# ════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ════════════════════════════════════════════════════════════════════

def _parse_args():
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Generate the weekly OEE DOCX/PDF report for one site.")

    parser.add_argument(
        "--site",
        default=os.environ.get("SITE_ID"),
        help='Site id as it appears under "sites" in config.json (or set SITE_ID).',
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_PATH", str(Path(__file__).resolve().parent / "config.json")),
        help="Path to config.json (or set CONFIG_PATH).",
    )
    parser.add_argument(
        "--week",
        type=int,
        required=True,
        help="ISO week number to generate the report for.",
    )

    args, _unknown = parser.parse_known_args()
    return args


if __name__ == "__main__":
    _args = _parse_args()

    if not _args.site:
        raise ConfigError(
            "No site specified. Pass --site <site_id> or set the SITE_ID environment "
            "variable. See config.json for the list of configured sites."
        )

    _config_manager = ConfigManager(_args.config)
    generate_report_docx(_args.site, _args.week, _config_manager)