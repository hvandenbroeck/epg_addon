"""TinyDB logging handler for storing and filtering logs."""
import logging
from datetime import datetime
from tinydb import TinyDB, Query
import threading
import queue
import time
import atexit


class TinyDBLoggingHandler(logging.Handler):
    """Custom logging handler that stores logs in TinyDB with filtering capabilities.
    
    Uses a background thread and queue to avoid blocking the application during logging.
    """
    
    def __init__(self, db_path='logs.json', max_records=5000, flush_interval=2.0):
        """Initialize the TinyDB logging handler.
        
        Args:
            db_path: Path to the TinyDB database file (default: logs.json)
            max_records: Maximum number of log records to keep (oldest are removed)
            flush_interval: How often to flush queued logs to DB (seconds)
        """
        super().__init__()
        self.db_path = db_path
        self.max_records = max_records
        self.flush_interval = flush_interval
        self.log_queue = queue.Queue(maxsize=10000)  # Limit queue size to avoid memory issues
        self.shutdown_event = threading.Event()
        self.last_trim_time = 0
        self.trim_interval = 60  # Only trim every 60 seconds
        
        # Start background worker thread
        self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="TinyDBLogWorker")
        self.worker_thread.start()
        
        # Register cleanup on exit
        atexit.register(self.close)
        
    def _worker(self):
        """Background worker that processes queued log records."""
        batch = []
        last_flush = time.time()
        
        while not self.shutdown_event.is_set():
            try:
                # Try to get a log record with timeout
                try:
                    record = self.log_queue.get(timeout=0.5)
                    if record is None:  # Shutdown signal
                        break
                    batch.append(record)
                except queue.Empty:
                    pass
                
                # Flush batch if interval elapsed or batch is large enough
                now = time.time()
                if batch and (now - last_flush >= self.flush_interval or len(batch) >= 100):
                    self._flush_batch(batch)
                    batch = []
                    last_flush = now
                    
            except Exception as e:
                # Log errors to stderr to avoid infinite loop
                import sys
                print(f"Error in TinyDB log worker: {e}", file=sys.stderr)
                batch = []  # Clear batch on error
        
        # Flush remaining logs on shutdown
        if batch:
            self._flush_batch(batch)
    
    def _flush_batch(self, batch):
        """Flush a batch of log records to TinyDB."""
        if not batch:
            return
            
        try:
            with TinyDB(self.db_path) as db:
                logs_table = db.table('logs')
                
                # Insert all logs in batch
                log_entries = []
                for record in batch:
                    log_entry = {
                        'timestamp': datetime.fromtimestamp(record.created).isoformat(),
                        'level': record.levelname,
                        'logger': record.name,
                        'module': record.module,
                        'filename': record.filename,
                        'funcName': record.funcName,
                        'lineno': record.lineno,
                        'message': self.format(record),
                        'pathname': record.pathname,
                    }
                    
                    # Add exception info if present
                    if record.exc_info:
                        log_entry['exc_info'] = self.formatter.formatException(record.exc_info)
                    
                    log_entries.append(log_entry)
                
                # Batch insert
                logs_table.insert_multiple(log_entries)
                
                # Trim old records periodically (not on every flush)
                now = time.time()
                if now - self.last_trim_time > self.trim_interval:
                    self._trim_logs(logs_table)
                    self.last_trim_time = now
                    
        except Exception as e:
            import sys
            print(f"Error flushing logs to TinyDB: {e}", file=sys.stderr)
    
    def _trim_logs(self, logs_table):
        """Trim old logs to stay within max_records limit."""
        try:
            total_logs = len(logs_table)
            if total_logs > self.max_records:
                # Get all records sorted by timestamp
                all_logs = logs_table.all()
                all_logs.sort(key=lambda x: x.get('timestamp', ''))
                
                # Remove oldest records
                records_to_remove = total_logs - self.max_records
                doc_ids_to_remove = [all_logs[i].doc_id for i in range(records_to_remove)]
                logs_table.remove(doc_ids=doc_ids_to_remove)
        except Exception as e:
            import sys
            print(f"Error trimming logs: {e}", file=sys.stderr)
    
    def emit(self, record):
        """Queue a log record for background processing."""
        try:
            # Non-blocking put with immediate failure if queue is full
            self.log_queue.put_nowait(record)
        except queue.Full:
            # Silently drop logs if queue is full to avoid blocking
            pass
        except Exception:
            self.handleError(record)
    
    def close(self):
        """Cleanup and flush remaining logs on handler close."""
        if not self.shutdown_event.is_set():
            self.shutdown_event.set()
            self.log_queue.put(None)  # Shutdown signal
            self.worker_thread.join(timeout=5)  # Wait up to 5 seconds for worker to finish
        super().close()
