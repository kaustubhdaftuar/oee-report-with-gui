# pipeline/queue_manager.py

"""
QueueManager - FIFO job queue with worker thread

Manages sequential processing of report generation jobs.
Multiple users can submit jobs → jobs are queued and processed one-by-one.

Key Features:
- FIFO queue (first in, first out)
- Worker thread (runs jobs sequentially)
- Job status tracking (queued, running, complete, failed)
- Status callbacks (for real-time frontend updates)
- Job result storage

Key Methods:
- submit_job(site_id, week, callback) → job_id
- get_job_status(job_id) → dict
- wait_for_job(job_id, timeout=None) → result
"""

import threading
import queue
import uuid
from datetime import datetime
from typing import Dict, Any, Callable, Optional
from enum import Enum


class JobStatus(Enum):
    """Job status constants."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job:
    """Represents a single report generation job."""
    
    def __init__(self, job_id: str, site_id: str, week: int, callback: Optional[Callable] = None):
        """
        Initialize a job.
        
        Args:
            job_id: Unique job identifier
            site_id: Site identifier (e.g., "jk_cement_aligarh")
            week: ISO week number
            callback: Optional callback function for status updates
        """
        
        self.job_id = job_id
        self.site_id = site_id
        self.week = week
        self.callback = callback
        
        # Job state
        self.status = JobStatus.QUEUED
        self.progress = 0  # 0-100
        self.message = "Queued for processing"
        
        self.started_at = None
        self.completed_at = None
        self.result = None
        self.error = None
    
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert job to dictionary for API response."""
        
        return {
            "job_id": self.job_id,
            "site_id": self.site_id,
            "week": self.week,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "error": self.error
        }
    
    
    def update_status(self, status: JobStatus, progress: int = None, message: str = None):
        """Update job status and optionally progress/message."""
        
        self.status = status
        
        if progress is not None:
            self.progress = progress
        
        if message is not None:
            self.message = message
        
        # Call callback if provided
        if self.callback:
            try:
                self.callback(self.to_dict())
            except Exception as e:
                print(f"[{self.job_id}] Callback error: {e}")


class QueueManager:
    """
    ✅ SEQUENTIAL: Process jobs one-by-one using a worker thread.
    
    Multiple users can submit jobs concurrently.
    Jobs are queued and processed sequentially by the worker thread.
    
    Example usage:
    ```python
    queue_manager = QueueManager(
        pipeline_func=run_report_pipeline,
        num_workers=1  # Sequential (always 1 for this use case)
    )
    
    # Start the worker
    queue_manager.start()
    
    # Submit a job
    job_id = queue_manager.submit_job(
        site_id="jk_cement_aligarh",
        week=36,
        callback=lambda status: print(f"Status: {status['progress']}%")
    )
    
    # Poll job status
    status = queue_manager.get_job_status(job_id)
    print(f"Status: {status['status']}, Progress: {status['progress']}%")
    
    # Wait for completion (blocking)
    result = queue_manager.wait_for_job(job_id, timeout=600)
    print(f"Result: {result}")
    ```
    """
    
    def __init__(
        self,
        pipeline_func: Callable,
        config_manager,
        num_workers: int = 1,
        max_queue_size: int = 100
    ):
        """
        Initialize QueueManager.
        
        Args:
            pipeline_func: Function to run for each job.
                          Should accept (site_id, week, config_manager, status_callback).
                          Returns result dict.
            config_manager: ConfigManager instance (passed to pipeline_func)
            num_workers: Number of worker threads (default 1 for sequential)
            max_queue_size: Maximum jobs in queue (older jobs dropped if exceeded)
        """
        
        self.pipeline_func = pipeline_func
        self.config_manager = config_manager
        self.num_workers = max(1, num_workers)  # Always >= 1
        self.max_queue_size = max_queue_size
        
        # Job queue (thread-safe)
        self.job_queue = queue.Queue(maxsize=0)
        
        # Job storage (job_id → Job object)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        
        # Worker thread
        self.workers = []
        self.running = False
        
        print(f"✓ QueueManager initialized")
        print(f"  Workers: {self.num_workers}")
        print(f"  Max queue size: {self.max_queue_size}")
    
    
    # ════════════════════════════════════════════════════════════════════════
    # JOB SUBMISSION
    # ════════════════════════════════════════════════════════════════════════
    
    def submit_job(
        self,
        site_id: str,
        week: int,
        callback: Optional[Callable] = None
    ) -> str:
        """
        Submit a new job for processing.
        
        ✅ THREAD-SAFE: Can be called from multiple threads (Flask requests).
        
        Args:
            site_id: Site identifier
            week: ISO week number
            callback: Optional function(status_dict) called on status updates
        
        Returns:
            job_id: Unique job identifier
        
        Raises:
            ValueError: If site_id invalid
            queue.Full: If queue is full (shouldn't happen with maxsize=0)
        """
        
        # Validate site
        try:
            self.config_manager.get_site_config(site_id)
        except ValueError:
            raise ValueError(f"Invalid site_id: {site_id}")
        
        # Create job
        job_id = str(uuid.uuid4())[:8]  # Short ID for user-facing URLs
        job = Job(job_id, site_id, week, callback)
        
        # Store job
        with self.jobs_lock:
            self.jobs[job_id] = job
            
            # Check queue size (warn if large)
            if len(self.jobs) > self.max_queue_size:
                print(f"⚠ Queue size exceeded ({len(self.jobs)}), oldest jobs may be dropped")
        
        # Add to queue
        self.job_queue.put(job)
        
        print(f"[{job_id}] Job submitted: {site_id} week {week}")
        print(f"  Queue size: {self.job_queue.qsize()}")
        
        return job_id
    
    
    # ════════════════════════════════════════════════════════════════════════
    # JOB STATUS
    # ════════════════════════════════════════════════════════════════════════
    
    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Get current status of a job.
        
        ✅ THREAD-SAFE: Can be called from multiple threads.
        
        Args:
            job_id: Job identifier
        
        Returns:
            dict: Job status (status, progress, message, result, error, etc.)
        
        Raises:
            KeyError: If job_id not found
        """
        
        with self.jobs_lock:
            if job_id not in self.jobs:
                raise KeyError(f"Job not found: {job_id}")
            
            job = self.jobs[job_id]
            return job.to_dict()
    
    
    def list_jobs(self, status_filter: Optional[str] = None) -> list:
        """
        List all jobs (optionally filtered by status).
        
        Args:
            status_filter: Optional status to filter by (e.g., "running", "failed")
        
        Returns:
            list of job dicts
        """
        
        with self.jobs_lock:
            jobs = list(self.jobs.values())
            
            if status_filter:
                jobs = [j for j in jobs if j.status.value == status_filter]
            
            return [j.to_dict() for j in jobs]
    
    
    def wait_for_job(self, job_id: str, timeout: Optional[float] = None) -> Any:
        """
        Block until job completes or times out.
        
        Args:
            job_id: Job identifier
            timeout: Timeout in seconds (None = wait forever)
        
        Returns:
            Job result dict
        
        Raises:
            TimeoutError: If timeout exceeded
            KeyError: If job not found
        """
        
        import time
        
        start_time = time.time()
        
        while True:
            with self.jobs_lock:
                if job_id not in self.jobs:
                    raise KeyError(f"Job not found: {job_id}")
                
                job = self.jobs[job_id]
                
                if job.status in (JobStatus.COMPLETE, JobStatus.FAILED):
                    return job.to_dict()
            
            # Check timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")
            
            # Sleep before next check
            time.sleep(0.5)
    
    
    # ════════════════════════════════════════════════════════════════════════
    # WORKER THREAD
    # ════════════════════════════════════════════════════════════════════════
    
    def _worker_loop(self, worker_id: int):
        """
        Worker thread main loop.
        
        Continuously processes jobs from the queue.
        
        Args:
            worker_id: Worker identifier (for logging)
        """
        
        print(f"[Worker {worker_id}] Started")
        
        while self.running:
            try:
                # Get next job (with timeout to check running flag regularly)
                try:
                    job = self.job_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Mark job as running
                with self.jobs_lock:
                    job.update_status(
                        JobStatus.RUNNING,
                        progress=10,
                        message="Starting pipeline..."
                    )
                    job.started_at = datetime.now()
                
                print(f"[{job.job_id}] Starting: {job.site_id} week {job.week}")
                
                try:
                    # Run pipeline function
                    # Pipeline function should accept status_callback
                    def status_callback(message, progress=None):
                        with self.jobs_lock:
                            job.update_status(
                                JobStatus.RUNNING,
                                progress=progress or job.progress,
                                message=message
                            )
                    
                    result = self.pipeline_func(
                        site_id=job.site_id,
                        week=job.week,
                        config_manager=self.config_manager,
                        progress_callback=status_callback
                    )
                    
                    # Mark job as complete
                    with self.jobs_lock:
                        job.result = result
                        job.update_status(
                            JobStatus.COMPLETE,
                            progress=100,
                            message="Pipeline complete"
                        )
                        job.completed_at = datetime.now()
                    
                    print(f"[{job.job_id}] ✓ Complete")
                
                except Exception as e:
                    # Mark job as failed
                    error_msg = str(e)
                    
                    with self.jobs_lock:
                        job.error = error_msg
                        job.update_status(
                            JobStatus.FAILED,
                            message=f"Pipeline failed: {error_msg}"
                        )
                        job.completed_at = datetime.now()
                    
                    print(f"[{job.job_id}] ✗ Failed: {error_msg}")
                    import traceback
                    traceback.print_exc()
                
                finally:
                    # Mark job as processed in queue
                    self.job_queue.task_done()
            
            except Exception as e:
                print(f"[Worker {worker_id}] Unexpected error: {e}")
                import traceback
                traceback.print_exc()
        
        print(f"[Worker {worker_id}] Stopped")
    
    
    def start(self):
        """Start worker threads."""
        
        if self.running:
            print("QueueManager already running")
            return
        
        self.running = True
        
        for i in range(self.num_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                daemon=False
            )
            worker.start()
            self.workers.append(worker)
        
        print(f"✓ QueueManager started ({self.num_workers} worker(s))")
    
    
    def stop(self, wait=True, timeout=30):
        """
        Stop worker threads.
        
        Args:
            wait: Wait for workers to finish current jobs
            timeout: Timeout for shutdown (seconds)
        """
        
        if not self.running:
            print("QueueManager not running")
            return
        
        print(f"Stopping QueueManager...")
        
        self.running = False
        
        if wait:
            # Wait for all jobs to complete
            try:
                self.job_queue.join()
            except:
                pass
        
        # Wait for workers to finish
        for i, worker in enumerate(self.workers):
            worker.join(timeout=timeout)
            if worker.is_alive():
                print(f"⚠ Worker {i} did not stop cleanly")
        
        self.workers.clear()
        print(f"✓ QueueManager stopped")
    
    
    # ════════════════════════════════════════════════════════════════════════
    # DEBUG
    # ════════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        
        with self.jobs_lock:
            jobs_by_status = {}
            for job in self.jobs.values():
                status = job.status.value
                jobs_by_status[status] = jobs_by_status.get(status, 0) + 1
            
            return {
                "queue_size": self.job_queue.qsize(),
                "total_jobs": len(self.jobs),
                "jobs_by_status": jobs_by_status,
                "workers_running": self.num_workers if self.running else 0
            }


if __name__ == "__main__":
    # Test QueueManager (requires real pipeline_func)
    print("QueueManager module loaded")
    print("Import and use: from queue_manager import QueueManager, JobStatus")