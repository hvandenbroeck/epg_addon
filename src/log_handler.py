"""TinyDB logging handler for storing and filtering logs."""
import logging
from datetime import datetime
from tinydb import TinyDB, Query
from threading import Lock


class TinyDBLoggingHandler(logging.Handler):
    """Custom logging handler that stores logs in TinyDB with filtering capabilities."""
    
    def __init__(self, db_path='db.json', max_records=5000):
        """Initialize the TinyDB logging handler.
        
        Args:
            db_path: Path to the TinyDB database file
            max_records: Maximum number of log records to keep (oldest are removed)
        """
        super().__init__()
        self.db_path = db_path
        self.max_records = max_records
        self.lock = Lock()
        
    def emit(self, record):
        """Emit a log record to TinyDB."""
        try:
            with self.lock:
                with TinyDB(self.db_path) as db:
                    logs_table = db.table('logs')
                    
                    # Create log entry
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
                    
                    # Insert log entry
                    logs_table.insert(log_entry)
                    
                    # Trim old records if we exceed max_records
                    if len(logs_table) > self.max_records:
                        # Get all records sorted by timestamp
                        all_logs = logs_table.all()
                        all_logs.sort(key=lambda x: x.get('timestamp', ''))
                        
                        # Remove oldest records
                        records_to_remove = len(all_logs) - self.max_records
                        for i in range(records_to_remove):
                            logs_table.remove(doc_ids=[all_logs[i].doc_id])
                            
        except Exception as e:
            # Fallback to stderr if we can't write to DB
            self.handleError(record)
