"""
Flask API Server - OEE Report Generation Web API

Provides REST endpoints for the web GUI to submit jobs, track progress, and download reports.

Endpoints:
- GET  /api/health                          → Server status
- GET  /api/sites                           → List available sites
- POST /api/generate-report                 → Submit report generation job
- GET  /api/job-status/<job_id>            → Get job status and progress
- GET  /api/jobs                            → List all jobs
- GET  /api/download/<site_id>/<week>      → Download generated PDF report
- GET  /api/queue-stats                     → Get queue statistics

Usage:
```bash
cd project/
python -m web_gui.app --config config.json --port 5000
```

Or:
```bash
cd project/web_gui/
python app.py --config ../config.json --port 5000
```
"""

import json
import sys
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import argparse
import signal


def make_json_safe(value):
    """Convert pipeline/queue values into JSON-serializable values."""
    if isinstance(value, Path):
        return str(value)

    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]

    # Handles datetime and other objects returned by the pipeline.
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


# ✅ Add parent directory to path so we can import from sibling pipeline/ folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_FILE = Path(__file__).resolve().parent / "index.html"

sys.path.insert(0, str(PROJECT_ROOT))

# ✅ Now import from pipeline (sibling folder)
from pipeline.config_manager import ConfigManager
from pipeline.queue_manager import QueueManager, JobStatus
from pipeline.run_pipeline import run_full_pipeline


# ════════════════════════════════════════════════════════════════════════════
# FLASK APP INITIALIZATION
# ════════════════════════════════════════════════════════════════════════════

def create_app(config_path: str = None):
    """
    Create and configure Flask app.
    
    Args:
        config_path: Path to config.json (relative to project root)
    
    Returns:
        Flask app instance with all routes configured
    """
    
    if config_path is None:
        config_path = str(PROJECT_ROOT / "config.json")
    else:
        config_path = str(Path(config_path).expanduser())
        if not Path(config_path).is_absolute():
            config_path = str((Path.cwd() / config_path).resolve())

    app = Flask(__name__, static_folder='static', template_folder='templates')

    @app.route("/", methods=["GET"])
    def frontend():
        if not FRONTEND_FILE.exists():
            return jsonify({"error": f"Frontend file not found: {FRONTEND_FILE}"}), 500
        return send_file(str(FRONTEND_FILE), mimetype="text/html")
    CORS(app)  # Enable CORS for all routes
    
    # Load config
    try:
        config = ConfigManager(config_path)
        print(f"✓ Config loaded: {config_path}")
    except Exception as e:
        print(f"✗ Error loading config: {e}")
        raise
    
    # Create queue manager
    # Queue will call run_full_pipeline() for each job
    queue = QueueManager(
        pipeline_func=run_full_pipeline,
        config_manager=config,
        num_workers=1,  # Sequential processing
        max_queue_size=100
    )
    queue.start()
    print("✓ Queue manager started")
    
    # Store in app context
    app.config["config"] = config
    app.config["queue"] = queue
    
    # ════════════════════════════════════════════════════════════════════════
    # ROUTES
    # ════════════════════════════════════════════════════════════════════════
    
    @app.route("/api/health", methods=["GET"])
    def health_check():
        """
        Server health check.
        
        Returns:
            {
                "status": "ok",
                "queue": {
                    "queue_size": 0,
                    "total_jobs": 5,
                    "jobs_by_status": {"complete": 4, "failed": 1},
                    "workers_running": 1
                }
            }
        """
        stats = app.config["queue"].get_stats()
        return jsonify({
            "status": "ok",
            "queue": stats
        }), 200
    
    
    @app.route("/api/sites", methods=["GET"])
    def list_sites():
        """
        Get list of available sites.
        
        Returns:
            [
                {
                    "site_id": "jk_cement_aligarh",
                    "name": "JK Cement",
                    "location": "Aligarh",
                    "plc_count": 2,
                    "shift_count": 3
                },
                ...
            ]
        """
        
        config = app.config["config"]
        sites = []
        
        for site_id in config.get_active_sites():
            site_config = config.get_site_config(site_id)
            sites.append({
                "site_id": site_id,
                "name": site_config.get("name"),
                "location": site_config.get("location"),
                "plc_count": len(site_config.get("plc_config", [])),
                "shift_count": len(site_config.get("shifts", []))
            })
        
        return jsonify(sites), 200
    
    
    @app.route("/api/generate-report", methods=["POST"])
    def submit_report():
        """
        Submit a report generation job.
        
        Request body:
        {
            "site_id": "jk_cement_aligarh",
            "week": 36
        }
        
        Returns:
            {
                "job_id": "a1b2c3d4",
                "site_id": "jk_cement_aligarh",
                "week": 36,
                "status": "queued",
                "progress": 0,
                "message": "Queued for processing"
            }
        """
        
        try:
            data = request.get_json() or {}
            site_id = data.get("site_id")
            week = data.get("week")
            
            if not site_id or week is None:
                return jsonify({
                    "error": "Missing required fields: site_id, week"
                }), 400
            
            # Validate week
            try:
                week = int(week)
                if week < 1 or week > 53:
                    raise ValueError("Week must be 1-53")
            except (ValueError, TypeError):
                return jsonify({
                    "error": "Invalid week (must be integer 1-53)"
                }), 400
            
            # Validate site exists
            config = app.config["config"]
            try:
                config.get_site_config(site_id)
            except ValueError:
                return jsonify({
                    "error": f"Site not found: {site_id}"
                }), 400
            
            # Submit job
            queue = app.config["queue"]
            job_id = queue.submit_job(site_id, week)
            status = queue.get_job_status(job_id)
            
            return jsonify(status), 202  # 202 Accepted
        
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    
    @app.route("/api/job-status/<job_id>", methods=["GET"])
    def get_job_status(job_id):
        """
        Get status of a job.
        
        Returns:
            {
                "job_id": "a1b2c3d4",
                "site_id": "jk_cement_aligarh",
                "week": 36,
                "status": "running",
                "progress": 50,
                "message": "Generating charts...",
                "result": {
                    "success": true,
                    "paths": {...},
                    "metrics": {...}
                },
                "error": null
            }
        """
        
        try:
            queue = app.config["queue"]
            status = queue.get_job_status(job_id)
            status = make_json_safe(status)
            
            return jsonify(status), 200
        
        except KeyError:
            return jsonify({"error": f"Job not found: {job_id}"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    
    @app.route("/api/jobs", methods=["GET"])
    def list_jobs():
        """
        List all jobs, optionally filtered by status.
        
        Query params:
        - status: Filter by status (queued, running, complete, failed)
        - limit: Maximum jobs to return (default 100)
        
        Returns:
            {
                "total": 5,
                "jobs": [...]
            }
        """
        
        try:
            queue = app.config["queue"]
            status_filter = request.args.get("status")
            
            jobs = queue.list_jobs(status_filter=status_filter)
            
            # Limit results
            limit = request.args.get("limit", default=100, type=int)
            jobs = jobs[-limit:]  # Last N jobs
            
            return jsonify({
                "total": len(jobs),
                "jobs": make_json_safe(jobs)
            }), 200
        
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    
    @app.route("/api/download/<site_id>/<int:week>", methods=["GET"])
    def download_report(site_id, week):
        """
        Download generated PDF report.
        
        Returns:
            PDF file binary (application/pdf) or error JSON
        """
        
        try:
            config = app.config["config"]
            paths = config.get_paths(site_id, week)
            
            pdf_path = paths["report_pdf"]
            
            if not pdf_path.exists():
                return jsonify({
                    "error": f"Report not found for {site_id} week {week}. "
                             f"Generate it first via /api/generate-report"
                }), 404
            
            # Send file
            return send_file(
                str(pdf_path),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=f"{site_id}_week_{week}_report.pdf"
            )
        
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    
    @app.route("/api/queue-stats", methods=["GET"])
    def queue_stats():
        """
        Get queue statistics.
        
        Returns:
            {
                "queue_size": 2,
                "total_jobs": 15,
                "jobs_by_status": {
                    "complete": 10,
                    "running": 1,
                    "queued": 2,
                    "failed": 2
                },
                "workers_running": 1
            }
        """
        
        try:
            queue = app.config["queue"]
            stats = queue.get_stats()
            
            return jsonify(stats), 200
        
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    
    @app.route("/api/config", methods=["GET"])
    def get_config():
        """
        Get public configuration (for frontend).
        
        Returns:
            {
                "sites": [
                    {"site_id": "...", "name": "...", "location": "..."},
                    ...
                ]
            }
        """
        
        try:
            config = app.config["config"]
            
            sites = []
            for site_id in config.get_active_sites():
                site_cfg = config.get_site_config(site_id)
                sites.append({
                    "site_id": site_id,
                    "name": site_cfg.get("name"),
                    "location": site_cfg.get("location")
                })
            
            return jsonify({
                "sites": sites
            }), 200
        
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    
    # ════════════════════════════════════════════════════════════════════════
    # ERROR HANDLERS
    # ════════════════════════════════════════════════════════════════════════
    
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Endpoint not found"}), 404
    
    
    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "Internal server error"}), 500
    
    
    # ════════════════════════════════════════════════════════════════════════
    # GRACEFUL SHUTDOWN
    # ════════════════════════════════════════════════════════════════════════
    
    def shutdown_handler(sig, frame):
        print("\n\nShutting down gracefully...")
        queue.stop(wait=True, timeout=30)
        print("✓ Queue stopped")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    return app


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OEE Report API Server")
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config.json"),
        help="Path to config.json",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    
    args = parser.parse_args()
    
    # Create app
    app = create_app(args.config)
    
    # Run server
    print(f"\n{'='*70}")
    print(f"OEE Report API Server")
    print(f"{'='*70}")
    print(f"Config: {args.config}")
    print(f"Listening on: http://{args.host}:{args.port}")
    print(f"Debug: {args.debug}")
    print(f"{'='*70}\n")
    
    print("Available endpoints:")
    print("  GET  /api/health")
    print("  GET  /api/sites")
    print("  POST /api/generate-report")
    print("  GET  /api/job-status/<job_id>")
    print("  GET  /api/jobs")
    print("  GET  /api/download/<site_id>/<week>")
    print("  GET  /api/queue-stats")
    print("  GET  /api/config\n")
    
    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=False  # Don't reload (would restart queue)
    )