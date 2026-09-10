# pipeline/config_manager.py

"""
ConfigManager - Centralized configuration and path management

This class loads config.json once and provides all modules with:
1. Site configurations (sensor keywords, PLC configs, etc.)
2. Elasticsearch connection details
3. LLM configuration
4. Path resolution for all files (input, output, working data, charts, etc.)

Key Methods:
- get_site_config(site_id) → dict
- get_es_config() → dict
- get_llm_config() → dict
- get_paths(site_id, week) → dict
- get_active_sites() → list
"""

import json
from pathlib import Path
from typing import Dict, Any, List


class ConfigManager:
    """
    ✅ CENTRALIZED: Load config once, provide to all modules.
    
    All file paths are resolved dynamically based on:
    - Project root directory
    - Site configuration
    - Week number
    
    Example usage:
    ```python
    config = ConfigManager("config.json")
    
    # Get site configuration
    site_config = config.get_site_config("jk_cement_aligarh")
    
    # Get all paths for a site and week
    paths = config.get_paths("jk_cement_aligarh", week=36)
    print(paths["excel_file"])      # working_data/jk_cement_aligarh/jk_cement_aligarh_week_36.xlsx
    print(paths["chart_dir"])       # output/jk_cement_aligarh/36/charts
    print(paths["report_pdf"])      # output/jk_cement_aligarh/36/report.pdf
    ```
    """
    
    def __init__(self, config_path: str = "config.json"):
        """
        Initialize ConfigManager by loading config.json.
        
        Args:
            config_path: Path to config.json (relative to current directory or absolute)
        
        Raises:
            FileNotFoundError: If config.json not found
            json.JSONDecodeError: If config.json is invalid JSON
        """
        
        config_file = Path(config_path)
        
        if not config_file.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_file.absolute()}\n"
                f"Make sure config.json is in the working directory."
            )
        
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in config.json: {e.msg}",
                e.doc,
                e.pos
            )
        
        # Determine project root (usually parent of config.json)
        self.project_root = config_file.parent.resolve()
        
        # Validate config structure
        self._validate_config()
        
        print(f"✓ ConfigManager initialized")
        print(f"  Config: {config_file.absolute()}")
        print(f"  Project root: {self.project_root}")
        print(f"  Active sites: {len(self.get_active_sites())}")
    
    
    def _validate_config(self):
        """Validate that config has required sections."""
        
        required = ["es_url", "api_key", "sites", "llm"]
        
        for key in required:
            if key not in self.config:
                raise ValueError(f"Config missing required key: {key}")
        
        if not self.config.get("sites"):
            raise ValueError("Config must have at least one site")
        
        for site in self.config["sites"]:
            if "site_id" not in site:
                raise ValueError("Each site must have a site_id")
    
    
    # ════════════════════════════════════════════════════════════════════════
    # ELASTICSEARCH & LLM CONFIG
    # ════════════════════════════════════════════════════════════════════════
    
    def get_es_config(self) -> Dict[str, Any]:
        """
        Get Elasticsearch configuration.
        
        Returns:
            dict: {"url": "...", "api_key": "..."}
        """
        
        return {
            "url": self.config["es_url"],
            "api_key": self.config["api_key"]
        }
    
    
    def get_llm_config(self) -> Dict[str, Any]:
        """
        Get LLM configuration.
        
        Returns:
            dict: LLM configuration (provider, endpoint, model, timeout)
        """
        
        return self.config.get("llm", {})
    
    
    # ════════════════════════════════════════════════════════════════════════
    # SITE CONFIGURATION
    # ════════════════════════════════════════════════════════════════════════
    
    def get_site_config(self, site_id: str) -> Dict[str, Any]:
        """
        Get configuration for a specific site.
        
        Contains all site-specific values:
        - sensor_keywords, plc_config, excel_cell_mapping, etc.
        - template_path, output_folder, excel_path
        - formulas, kpi_thresholds, es_aggregation
        
        Args:
            site_id: Site identifier (e.g., "jk_cement_aligarh")
        
        Returns:
            dict: Site configuration
        
        Raises:
            ValueError: If site_id not found
        """
        
        for site in self.config["sites"]:
            if site["site_id"] == site_id:
                return site
        
        raise ValueError(
            f"Site '{site_id}' not found in config.json\n"
            f"Available sites: {[s['site_id'] for s in self.config['sites']]}"
        )
    
    
    def get_active_sites(self) -> List[str]:
        """
        Get list of active site IDs.
        
        If config.pipeline.active_sites is defined, return only those.
        Otherwise, return all site IDs.
        
        Returns:
            list: Active site IDs
        """
        
        pipeline_config = self.config.get("pipeline", {})
        active_sites = pipeline_config.get("active_sites")
        
        if active_sites:
            return active_sites
        
        # Default: all sites
        return [site["site_id"] for site in self.config["sites"]]
    
    
    # ════════════════════════════════════════════════════════════════════════
    # PATH RESOLUTION
    # ════════════════════════════════════════════════════════════════════════
    
    def get_paths(self, site_id: str, week: int) -> Dict[str, Path]:
        """
        ✅ DYNAMIC: Resolve all paths for a site and week.
        
        Returns all paths needed throughout the pipeline:
        - excel_file: Input Excel workbook
        - chart_dir: Output chart directory
        - analysis_json: Analysis JSON file
        - report_docx: Output DOCX report
        - report_pdf: Output PDF report
        - history_dir: KPI history snapshots
        
        All paths are resolved from:
        1. Site config values (excel_path, output_folder, template_path)
        2. Project root directory
        3. Week number (formatted as needed)
        
        Args:
            site_id: Site identifier (e.g., "jk_cement_aligarh")
            week: ISO week number (e.g., 36)
        
        Returns:
            dict of paths: {
                "excel_file": Path,
                "chart_dir": Path,
                "analysis_json": Path,
                "report_docx": Path,
                "report_pdf": Path,
                "history_dir": Path
            }
        
        Raises:
            ValueError: If site_id not found
        """
        
        site_config = self.get_site_config(site_id)
        
        # ════════════════════════════════════════════════════════════════════
        # WORKING DATA (inputs to analysis pipeline)
        # ════════════════════════════════════════════════════════════════════
        
        excel_dir = self.project_root / site_config["excel_path"]
        excel_filename = site_config["excel_filename"].format(week=week)
        excel_file = excel_dir / excel_filename
        
        # ════════════════════════════════════════════════════════════════════
        # OUTPUT (results of analysis pipeline)
        # ════════════════════════════════════════════════════════════════════
        
        output_dir = self.project_root / site_config["output_folder"]
        week_output_dir = output_dir / str(week)
        
        # Charts
        chart_dir = week_output_dir / "charts"
        
        # Analysis JSON (input for charts/docx generation)
        analysis_json = week_output_dir / "analysis.json"
        
        # Reports
        report_docx = week_output_dir / "report.docx"
        report_pdf = week_output_dir / "report.pdf"
        
        # ════════════════════════════════════════════════════════════════════
        # HISTORY (KPI snapshots for trend analysis)
        # ════════════════════════════════════════════════════════════════════
        
        history_dir = self.project_root / site_config["excel_path"] / "history"
        
        return {
            # Working data
            "excel_file": excel_file,
            "excel_dir": excel_dir,
            
            # Output (per week)
            "chart_dir": chart_dir,
            "analysis_json": analysis_json,
            "report_docx": report_docx,
            "report_pdf": report_pdf,
            "week_output_dir": week_output_dir,
            
            # History
            "history_dir": history_dir,
        }
    
    
    # ════════════════════════════════════════════════════════════════════════
    # CONVENIENCE METHODS
    # ════════════════════════════════════════════════════════════════════════
    
    def get_site_name(self, site_id: str) -> str:
        """Get human-readable site name."""
        site = self.get_site_config(site_id)
        return site.get("name", site_id)
    
    
    def get_site_location(self, site_id: str) -> str:
        """Get site location."""
        site = self.get_site_config(site_id)
        return site.get("location", "")
    
    
    def get_es_index(self, site_id: str) -> str:
        """Get Elasticsearch index pattern for site."""
        site = self.get_site_config(site_id)
        return site.get("es_index", "")
    
    
    def get_sensor_keywords(self, site_id: str) -> Dict[str, Any]:
        """Get sensor keyword configuration for site."""
        site = self.get_site_config(site_id)
        return site.get("sensor_keywords", {})
    
    
    def get_excel_cell_mapping(self, site_id: str) -> Dict[str, str]:
        """Get Excel cell mapping for site."""
        site = self.get_site_config(site_id)
        return site.get("excel_cell_mapping", {})
    
    
    def get_kpi_thresholds(self, site_id: str) -> Dict[str, float]:
        """Get KPI thresholds for site."""
        site = self.get_site_config(site_id)
        return site.get("kpi_thresholds", {})
    
    
    def get_plc_config(self, site_id: str) -> list:
        """Get PLC configuration for site."""
        site = self.get_site_config(site_id)
        return site.get("plc_config", [])
    
    
    def get_shifts(self, site_id: str) -> list:
        """Get shift configuration for site."""
        site = self.get_site_config(site_id)
        return site.get("shifts", [])
    
    
    # ════════════════════════════════════════════════════════════════════════
    # DEBUG
    # ════════════════════════════════════════════════════════════════════════
    
    def print_summary(self):
        """Print configuration summary."""
        
        print(f"\n{'='*70}")
        print(f"CONFIGURATION SUMMARY")
        print(f"{'='*70}\n")
        
        # Elasticsearch
        print(f"Elasticsearch:")
        print(f"  URL: {self.config['es_url'][:50]}...")
        print()
        
        # LLM
        llm = self.get_llm_config()
        print(f"LLM:")
        print(f"  Provider: {llm.get('provider', 'local')}")
        print(f"  Endpoint: {llm.get('endpoint', 'http://localhost:8000')}")
        print(f"  Model: {llm.get('model_name', 'tinyllama')}")
        print()
        
        # Sites
        print(f"Sites ({len(self.config['sites'])}):")
        for site in self.config["sites"]:
            print(f"  • {site['site_id']}")
            print(f"      Name: {site['name']}")
            print(f"      Location: {site['location']}")
            print(f"      Index: {site['es_index']}")
            print(f"      PLCs: {len(site.get('plc_config', []))}")
        
        print()


# ════════════════════════════════════════════════════════════════════════════
# TESTING
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Test ConfigManager
    try:
        config = ConfigManager("config.json")
        
        config.print_summary()
        
        # Test paths for JK Cement, week 36
        print(f"\nPaths for jk_cement_aligarh, week 36:")
        paths = config.get_paths("jk_cement_aligarh", 36)
        
        for key, path in paths.items():
            print(f"  {key}: {path}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()