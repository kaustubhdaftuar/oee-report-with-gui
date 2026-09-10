"""
Unified OEE Report Pipeline Orchestrator

Coordinates all 3 pipeline stages:
  Stage 1: Analysis (ES → Excel → OEE metrics)
  Stage 2: Charts (KPI, availability, downtime, quality, trend)
  Stage 3: Report (DOCX + PDF)

Usage:
  result = run_full_pipeline(
      site_id='jk_cement_aligarh',
      week=36,
      config_manager=config,
      progress_callback=lambda msg, progress: print(f"{progress}%: {msg}")
  )
"""

import logging
from typing import Callable, Dict, Any, Optional
from datetime import datetime

# Import pipeline stages (relative imports since we're in same package)
from .create_analysis import run_analysis_pipeline
from .generate_charts import generate_all_charts
from .generate_docx import generate_report_docx

logger = logging.getLogger(__name__)


def run_full_pipeline(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[Callable[[str, int], None]] = None,
    skip_stages: Optional[list] = None
) -> Dict[str, Any]:
    """
    Run complete OEE report pipeline for a single site/week.
    
    Args:
        site_id: Site identifier (must exist in config.json)
        week: ISO week number (1-53)
        config_manager: ConfigManager instance
        progress_callback: Optional callback(message, progress_percent)
        skip_stages: Optional list of stages to skip ['analysis', 'charts', 'report']
    
    Returns:
        {
            'success': bool,
            'site_id': str,
            'week': int,
            'paths': {
                'excel_file': str,
                'analysis_json': str,
                'charts': {...},
                'docx_file': str,
                'pdf_file': str
            },
            'metrics': {
                'oee': float,
                'availability': float,
                'performance': float,
                'quality': float,
                ...
            },
            'started_at': datetime,
            'completed_at': datetime,
            'error': Optional[str]
        }
    """
    
    skip_stages = skip_stages or []
    result = {
        'success': False,
        'site_id': site_id,
        'week': week,
        'paths': {},
        'metrics': {},
        'started_at': datetime.now(),
        'completed_at': None,
        'error': None
    }
    
    try:
        # Stage 1: Analysis (ES → Excel → analysis.json)
        if 'analysis' not in skip_stages:
            _update_progress(progress_callback, "Stage 1/3: Querying Elasticsearch...", 10)
            
            analysis, paths = run_analysis_pipeline(site_id, week, config_manager)
            
            result['paths'].update(paths)
            result['metrics'] = analysis
            
            _update_progress(progress_callback, "Stage 1/3: Analysis complete", 30)
            logger.info(f"✓ Stage 1 complete: {paths['analysis_json']}")
        
        # Stage 1.5: Generate LLM Insights (optional)
        if 'insights' not in skip_stages:
            _update_progress(progress_callback, "Stage 1.5/3: Generating AI insights...", 35)
            
            try:
                from .local_llm_client import generate_insights
                
                insights = generate_insights(
                    analysis,
                    config_manager,
                    progress_callback=lambda msg: _update_progress(progress_callback, msg, 35)
                )
                
                result['insights'] = insights
                _update_progress(progress_callback, "Stage 1.5/3: Insights complete", 40)
                logger.info("✓ Stage 1.5 complete: LLM insights generated")
            except Exception as e:
                logger.warning(f"LLM insights generation failed (will continue): {e}")
                result['insights'] = None
        
        # Stage 2: Charts (analysis.json → PNG charts)
        if 'charts' not in skip_stages:
            _update_progress(progress_callback, "Stage 2/3: Generating charts...", 40)
            
            charts = generate_all_charts(site_id, week, config_manager)
            
            result['paths']['charts'] = charts
            
            _update_progress(progress_callback, "Stage 2/3: Charts complete", 60)
            logger.info(f"✓ Stage 2 complete: {len(charts)} charts generated")
        
        # Stage 3: Report (template + charts → DOCX + PDF)
        if 'report' not in skip_stages:
            _update_progress(progress_callback, "Stage 3/3: Generating report...", 70)
            
            docx_path, pdf_path = generate_report_docx(site_id, week, config_manager)
            
            result['paths']['docx_file'] = docx_path
            result['paths']['pdf_file'] = pdf_path
            
            _update_progress(progress_callback, "Stage 3/3: Report complete", 90)
            logger.info(f"✓ Stage 3 complete: {pdf_path}")
        
        result['success'] = True
        result['completed_at'] = datetime.now()
        
        _update_progress(
            progress_callback,
            f"✓ Pipeline complete! ({result['completed_at'] - result['started_at']})",
            100
        )
        
        return result
    
    except Exception as e:
        logger.error(f"Pipeline failed: {str(e)}", exc_info=True)
        result['error'] = str(e)
        result['completed_at'] = datetime.now()
        _update_progress(progress_callback, f"✗ Error: {str(e)}", 0)
        return result


def run_analysis_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[Callable[[str, int], None]] = None
) -> Dict[str, Any]:
    """Run only Stage 1 (analysis) of the pipeline."""
    return run_full_pipeline(
        site_id, week, config_manager, progress_callback,
        skip_stages=['charts', 'report']
    )


def run_charts_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[Callable[[str, int], None]] = None
) -> Dict[str, Any]:
    """Run only Stage 2 (charts) of the pipeline."""
    return run_full_pipeline(
        site_id, week, config_manager, progress_callback,
        skip_stages=['analysis', 'report']
    )


def run_report_only(
    site_id: str,
    week: int,
    config_manager,
    progress_callback: Optional[Callable[[str, int], None]] = None
) -> Dict[str, Any]:
    """Run only Stage 3 (report generation) of the pipeline."""
    return run_full_pipeline(
        site_id, week, config_manager, progress_callback,
        skip_stages=['analysis', 'charts']
    )


def _update_progress(
    callback: Optional[Callable[[str, int], None]],
    message: str,
    progress: int
) -> None:
    """Helper to call progress callback if provided."""
    if callback:
        try:
            callback(message, progress)
        except Exception as e:
            logger.warning(f"Progress callback failed: {e}")
    else:
        logger.info(f"[{progress}%] {message}")


# ============================================================================
# Convenience function for batch processing
# ============================================================================

def run_batch_pipeline(
    site_week_list: list,
    config_manager,
    progress_callback: Optional[Callable[[str, int], None]] = None
) -> list:
    """
    Run pipeline for multiple site/week combinations.
    
    Args:
        site_week_list: List of (site_id, week) tuples
                       [(site_id, week), ...]
        config_manager: ConfigManager instance
        progress_callback: Optional callback(message, progress_percent)
    
    Returns:
        List of result dicts from run_full_pipeline()
    """
    results = []
    total = len(site_week_list)
    
    for idx, (site_id, week) in enumerate(site_week_list):
        batch_progress = int((idx / total) * 100)
        _update_progress(
            progress_callback,
            f"Processing {idx+1}/{total}: {site_id} week {week}",
            batch_progress
        )
        
        result = run_full_pipeline(site_id, week, config_manager)
        results.append(result)
    
    return results


if __name__ == '__main__':
    """
    Example usage:
    
    from pipeline.config_manager import ConfigManager
    from pipeline.run_pipeline import run_full_pipeline
    
    config = ConfigManager('config.json')
    
    result = run_full_pipeline(
        site_id='jk_cement_aligarh',
        week=36,
        config_manager=config,
        progress_callback=lambda msg, pct: print(f"{pct}%: {msg}")
    )
    
    if result['success']:
        print(f"✓ Report: {result['paths']['pdf_file']}")
        print(f"  OEE: {result['metrics']['oee']:.1f}%")
    else:
        print(f"✗ Error: {result['error']}")
    """
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    
    print(__doc__)