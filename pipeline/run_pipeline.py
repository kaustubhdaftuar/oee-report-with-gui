"""
Unified OEE Report Pipeline Orchestrator

Coordinates all pipeline stages:

  Stage 1: Analysis (ES → Excel → OEE metrics)
  Stage 1.5: AI Insights (LLM)
  Stage 2: Charts
  Stage 3: Report (DOCX + PDF)

Multi-site and multi-week safe.
"""

import logging
from typing import Callable, Dict, Any, Optional
from datetime import datetime


from .create_analysis import run_analysis_pipeline
from .generate_charts import generate_all_charts
from .generate_docx import generate_report_docx


logger = logging.getLogger(__name__)


def _update_progress(
    callback: Optional[Callable[[str, int], None]],
    message: str,
    progress: int,
) -> None:
    if callback:
        try:
            callback(message, progress)
        except Exception as e:
            logger.warning(
                f"Progress callback failed: {e}"
            )
    else:
        logger.info(
            f"[{progress}%] {message}"
        )


def run_full_pipeline(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[Callable[[str, int], None]] = None,
    skip_stages: Optional[list] = None,
    status_callback: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:

    if status_callback is None:
        status_callback = progress_callback

    callback = status_callback
    skip_stages = skip_stages or []

    result = {
        "success": False,
        "site_id": site_id,
        "week": week,
        "paths": {},
        "metrics": {},
        "insights": None,
        "started_at": datetime.now(),
        "completed_at": None,
        "error": None,
    }

    try:

        # ====================================================================
        # STAGE 1 — ANALYSIS
        # ====================================================================

        if "analysis" not in skip_stages:

            _update_progress(
                callback,
                "Stage 1/3: Querying Elasticsearch...",
                10,
            )

            analysis, paths = run_analysis_pipeline(
                site_id=site_id,
                week=week,
                config_manager=config_manager,
            )

            if not isinstance(analysis, dict):
                raise RuntimeError(
                    "Stage 1 returned invalid analysis data."
                )

            result["paths"].update(paths)
            result["metrics"] = analysis

            _update_progress(
                callback,
                "Stage 1/3: Analysis complete",
                30,
            )

            logger.info(
                f"✓ Stage 1 complete: "
                f"{paths.get('analysis_json', 'analysis.json')}"
            )

        else:

            logger.info(
                "Stage 1 skipped"
            )

            # When Stage 1 is skipped, Stage 1.5 will load the
            # existing site/week analysis.json itself.

        # ====================================================================
        # STAGE 1.5 — AI INSIGHTS
        # ====================================================================

        if "insights" not in skip_stages:

            _update_progress(
                callback,
                "Stage 1.5/3: Generating AI insights...",
                35,
            )

            try:

                from .local_llm_client import generate_insights

                # IMPORTANT FIX:
                #
                # Pass the freshly generated Stage-1 analysis object directly.
                # This prevents the LLM stage from accidentally reading an
                # old/wrong analysis.json from another site/week.
                #
                # site_id + week are still passed separately so output paths
                # remain:
                #
                # output/<site_id>/<week>/insights.json
                #
                insights = generate_insights(
                    analysis if "analysis" not in skip_stages else None,
                    site=site_id,
                    week=week,
                    config_manager=config_manager,
                    write_output=True,
                    progress_callback=lambda msg: _update_progress(
                        callback,
                        msg,
                        35,
                    ),
                )

                result["insights"] = insights

                _update_progress(
                    callback,
                    "Stage 1.5/3: Insights complete",
                    40,
                )

                logger.info(
                    "✓ Stage 1.5 complete: LLM insights generated"
                )

            except Exception as e:

                logger.error(
                    f"LLM insights generation failed: {e}",
                    exc_info=True,
                )

                # Keep the pipeline behavior: report generation may continue.
                # The error is exposed in logs and in result["error"] only if
                # the complete pipeline subsequently fails.
                result["insights"] = None

                _update_progress(
                    callback,
                    f"AI insights unavailable; continuing report generation: {e}",
                    40,
                )

        else:

            logger.info(
                "Stage 1.5 AI insights skipped"
            )

        # ====================================================================
        # STAGE 2 — CHARTS
        # ====================================================================

        if "charts" not in skip_stages:

            _update_progress(
                callback,
                "Stage 2/3: Generating charts...",
                40,
            )

            charts = generate_all_charts(
                site_id,
                week,
                config_manager,
            )

            result["paths"]["charts"] = charts

            _update_progress(
                callback,
                "Stage 2/3: Charts complete",
                60,
            )

            logger.info(
                f"✓ Stage 2 complete: "
                f"{len(charts)} charts generated"
            )

        else:

            logger.info(
                "Stage 2 charts skipped"
            )

        # ====================================================================
        # STAGE 3 — REPORT
        # ====================================================================

        if "report" not in skip_stages:

            _update_progress(
                callback,
                "Stage 3/3: Generating report...",
                70,
            )

            docx_path, pdf_path = generate_report_docx(
                site_id,
                week,
                config_manager,
            )

            result["paths"]["docx_file"] = docx_path
            result["paths"]["pdf_file"] = pdf_path

            _update_progress(
                callback,
                "Stage 3/3: Report complete",
                90,
            )

            logger.info(
                f"✓ Stage 3 complete: {pdf_path}"
            )

        else:

            logger.info(
                "Stage 3 report generation skipped"
            )

        # ====================================================================
        # SUCCESS
        # ====================================================================

        result["success"] = True
        result["completed_at"] = datetime.now()

        elapsed = (
            result["completed_at"]
            - result["started_at"]
        )

        _update_progress(
            callback,
            f"✓ Pipeline complete! ({elapsed})",
            100,
        )

        logger.info(
            f"✓ Pipeline complete: "
            f"{site_id}, week {week}"
        )

        return result

    except Exception as e:

        logger.error(
            f"Pipeline failed: {str(e)}",
            exc_info=True,
        )

        result["success"] = False
        result["error"] = str(e)
        result["completed_at"] = datetime.now()

        _update_progress(
            callback,
            f"✗ Error: {str(e)}",
            0,
        )

        return result


def run_analysis_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[
        Callable[[str, int], None]
    ] = None,
) -> Dict[str, Any]:

    return run_full_pipeline(
        site_id=site_id,
        week=week,
        config_manager=config_manager,
        progress_callback=progress_callback,
        skip_stages=[
            "insights",
            "charts",
            "report",
        ],
    )


def run_charts_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[
        Callable[[str, int], None]
    ] = None,
) -> Dict[str, Any]:

    return run_full_pipeline(
        site_id=site_id,
        week=week,
        config_manager=config_manager,
        progress_callback=progress_callback,
        skip_stages=[
            "analysis",
            "insights",
            "report",
        ],
    )


def run_insights_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[
        Callable[[str, int], None]
    ] = None,
) -> Dict[str, Any]:

    return run_full_pipeline(
        site_id=site_id,
        week=week,
        config_manager=config_manager,
        progress_callback=progress_callback,
        skip_stages=[
            "analysis",
            "charts",
            "report",
        ],
    )


def run_report_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[
        Callable[[str, int], None]
    ] = None,
) -> Dict[str, Any]:

    return run_full_pipeline(
        site_id=site_id,
        week=week,
        config_manager=config_manager,
        progress_callback=progress_callback,
        skip_stages=[
            "analysis",
            "insights",
            "charts",
        ],
    )


def run_batch_pipeline(
    site_week_list: list,
    config_manager,
    progress_callback: Optional[
        Callable[[str, int], None]
    ] = None,
) -> list:

    results = []
    total = len(site_week_list)

    if total == 0:
        return results

    for idx, (site_id, week) in enumerate(site_week_list):

        batch_progress = int(
            (idx / total) * 100
        )

        _update_progress(
            progress_callback,
            f"Processing {idx + 1}/{total}: "
            f"{site_id} week {week}",
            batch_progress,
        )

        result = run_full_pipeline(
            site_id=site_id,
            week=week,
            config_manager=config_manager,
            progress_callback=progress_callback,
        )

        results.append(result)

    return results


if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print(__doc__)
