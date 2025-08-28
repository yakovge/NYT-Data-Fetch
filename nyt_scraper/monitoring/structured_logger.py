"""Structured logging with JSONL format."""

import json
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from ..config import config


def add_timestamp(logger, method_name, event_dict):
    """Add ISO timestamp to log entries."""
    event_dict["timestamp"] = datetime.utcnow().isoformat()
    return event_dict


def add_hostname(logger, method_name, event_dict):
    """Add hostname to log entries."""
    event_dict["hostname"] = socket.gethostname()
    return event_dict


def add_request_id(logger, method_name, event_dict):
    """Add request ID if present in context."""
    context = event_dict.get("_context", {})
    if "request_id" in context:
        event_dict["request_id"] = context["request_id"]
    return event_dict


# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        add_timestamp,
        add_hostname,
        add_request_id,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)


class RotatingJSONLHandler(logging.Handler):
    """Custom handler for rotating JSONL log files."""
    
    def __init__(
        self,
        filename: Path,
        max_bytes: int = 1024 * 1024 * 1024,  # 1GB
        backup_count: int = 7
    ):
        super().__init__()
        self.filename = Path(filename)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.current_size = 0
        
        # Ensure log directory exists
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        
        # Get current file size if it exists
        if self.filename.exists():
            self.current_size = self.filename.stat().st_size
    
    def emit(self, record):
        """Emit a log record."""
        try:
            msg = self.format(record)
            
            # Check if rotation is needed
            if self.current_size + len(msg) > self.max_bytes:
                self.rotate()
            
            # Write log entry
            with open(self.filename, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
                self.current_size += len(msg) + 1
                
        except Exception:
            self.handleError(record)
    
    def rotate(self):
        """Rotate log files."""
        import gzip
        import shutil
        from datetime import datetime
        
        # Create archive filename with timestamp
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        archive_name = config.log_archive_dir / f"scraper_{timestamp}.jsonl.gz"
        
        # Compress current log file
        with open(self.filename, "rb") as f_in:
            with gzip.open(archive_name, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        # Clear current log file
        self.filename.unlink()
        self.current_size = 0
        
        # Clean up old archives
        self.cleanup_old_archives()
    
    def cleanup_old_archives(self):
        """Remove old archived logs."""
        archives = sorted(config.log_archive_dir.glob("*.jsonl.gz"))
        
        # Keep only the most recent backup_count archives
        if len(archives) > self.backup_count:
            for archive in archives[:-self.backup_count]:
                archive.unlink()


def setup_logging():
    """Set up structured logging configuration."""
    # Create custom handler
    handler = RotatingJSONLHandler(config.log_file)
    handler.setLevel(logging.INFO)
    
    # Add handler to root logger
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)
    
    # Also log to console in development
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        logging.root.addHandler(console_handler)


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a structured logger instance.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)


# Initialize logging on module import
setup_logging()


class LogContext:
    """Context manager for adding request context to logs."""
    
    def __init__(self, **kwargs):
        self.context = kwargs
        self.token = None
    
    def __enter__(self):
        self.token = structlog.contextvars.bind_contextvars(**self.context)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token:
            structlog.contextvars.unbind_contextvars(*self.context.keys())