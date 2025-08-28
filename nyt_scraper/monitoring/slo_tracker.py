"""Service Level Objectives (SLO) tracker for monitoring scraper performance against targets."""

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

from ..config import config
from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


class SLOType(Enum):
    """Types of SLOs being tracked."""
    DETERMINISTIC_SUCCESS_RATE = "deterministic_success_rate"
    AI_USAGE_RATE = "ai_usage_rate"
    FRESHNESS_LAG = "freshness_lag_minutes"
    AVAILABILITY = "availability"
    ERROR_RATE = "error_rate"
    RESPONSE_TIME = "response_time"


@dataclass
class SLOMetric:
    """Individual SLO metric measurement."""
    timestamp: datetime
    slo_type: SLOType
    value: float
    target: float
    is_met: bool
    error_budget_consumed: float
    additional_data: Dict[str, Any] = None


class SLOTracker:
    """Tracks Service Level Objectives and error budgets for the scraper."""
    
    def __init__(self, sqlite_manager=None):
        self.sqlite_manager = sqlite_manager
        self.slo_db_path = config.data_dir / "slo_metrics.db"
        self._lock = threading.RLock()
        
        # SLO targets from configuration
        self.slo_targets = {
            SLOType.DETERMINISTIC_SUCCESS_RATE: config.slo_deterministic_success_rate,
            SLOType.AI_USAGE_RATE: config.slo_ai_usage_rate,
            SLOType.FRESHNESS_LAG: config.slo_freshness_lag_minutes,
            SLOType.AVAILABILITY: 0.99,  # 99% uptime
            SLOType.ERROR_RATE: 0.05,    # Max 5% error rate
            SLOType.RESPONSE_TIME: 30.0  # Max 30 seconds average response time
        }
        
        # Error budget configuration (30-day rolling window)
        self.error_budget_window_days = 30
        self.error_budget_alert_threshold = 0.8  # Alert when 80% of error budget consumed
        
        self._init_slo_db()
        logger.info("slo_tracker_initialized", target_count=len(self.slo_targets))
    
    def _init_slo_db(self):
        """Initialize SLO tracking database."""
        try:
            with sqlite3.connect(self.slo_db_path) as conn:
                # SLO measurements table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS slo_measurements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        slo_type TEXT NOT NULL,
                        measured_value REAL NOT NULL,
                        target_value REAL NOT NULL,
                        is_met BOOLEAN NOT NULL,
                        error_budget_consumed REAL NOT NULL,
                        window_start TIMESTAMP,
                        window_end TIMESTAMP,
                        additional_data TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Error budget tracking
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS error_budget_status (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        slo_type TEXT NOT NULL,
                        budget_consumed_percent REAL NOT NULL,
                        budget_remaining_percent REAL NOT NULL,
                        window_start TIMESTAMP NOT NULL,
                        window_end TIMESTAMP NOT NULL,
                        total_measurements INTEGER,
                        failed_measurements INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # SLO alerts history
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS slo_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        slo_type TEXT NOT NULL,
                        alert_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        message TEXT NOT NULL,
                        measured_value REAL,
                        target_value REAL,
                        error_budget_consumed REAL,
                        resolved_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Indexes for performance
                conn.execute("CREATE INDEX IF NOT EXISTS idx_slo_measurements_timestamp ON slo_measurements(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_slo_measurements_type ON slo_measurements(slo_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_error_budget_timestamp ON error_budget_status(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_slo_alerts_timestamp ON slo_alerts(timestamp)")
                
        except Exception as e:
            logger.error("slo_db_init_failed", error=str(e))
    
    def record_deterministic_parsing_result(self, success: bool, total_attempts: int = None, window_hours: int = 1):
        """Record deterministic parsing success rate measurement."""
        try:
            with self._lock:
                window_end = datetime.utcnow()
                window_start = window_end - timedelta(hours=window_hours)
                
                # Calculate success rate from recent attempts
                if total_attempts is None:
                    # Query recent parsing attempts from main database if available
                    success_rate = self._calculate_recent_success_rate(window_start, window_end)
                else:
                    success_rate = 1.0 if success else 0.0
                
                target = self.slo_targets[SLOType.DETERMINISTIC_SUCCESS_RATE]
                is_met = success_rate >= target
                error_budget_consumed = max(0, (target - success_rate) / target) if not is_met else 0.0
                
                metric = SLOMetric(
                    timestamp=window_end,
                    slo_type=SLOType.DETERMINISTIC_SUCCESS_RATE,
                    value=success_rate,
                    target=target,
                    is_met=is_met,
                    error_budget_consumed=error_budget_consumed,
                    additional_data={"window_hours": window_hours, "total_attempts": total_attempts}
                )
                
                self._record_slo_metric(metric, window_start, window_end)
                self._check_slo_alerts(metric)
                
        except Exception as e:
            logger.error("deterministic_parsing_slo_record_failed", error=str(e))
    
    def record_ai_usage(self, ai_requests: int, total_requests: int, window_hours: int = 24):
        """Record AI usage rate measurement."""
        try:
            with self._lock:
                if total_requests == 0:
                    return  # No measurements to record
                
                window_end = datetime.utcnow()
                window_start = window_end - timedelta(hours=window_hours)
                
                ai_usage_rate = ai_requests / total_requests
                target = self.slo_targets[SLOType.AI_USAGE_RATE]
                is_met = ai_usage_rate <= target
                error_budget_consumed = max(0, (ai_usage_rate - target) / target) if not is_met else 0.0
                
                metric = SLOMetric(
                    timestamp=window_end,
                    slo_type=SLOType.AI_USAGE_RATE,
                    value=ai_usage_rate,
                    target=target,
                    is_met=is_met,
                    error_budget_consumed=error_budget_consumed,
                    additional_data={"ai_requests": ai_requests, "total_requests": total_requests, "window_hours": window_hours}
                )
                
                self._record_slo_metric(metric, window_start, window_end)
                self._check_slo_alerts(metric)
                
        except Exception as e:
            logger.error("ai_usage_slo_record_failed", error=str(e))
    
    def record_freshness_lag(self, article_publish_time: datetime, discovery_time: datetime):
        """Record article freshness lag measurement."""
        try:
            with self._lock:
                if not article_publish_time or not discovery_time:
                    return
                
                freshness_lag_minutes = (discovery_time - article_publish_time).total_seconds() / 60
                target = self.slo_targets[SLOType.FRESHNESS_LAG]
                is_met = freshness_lag_minutes <= target
                error_budget_consumed = max(0, (freshness_lag_minutes - target) / target) if not is_met else 0.0
                
                metric = SLOMetric(
                    timestamp=discovery_time,
                    slo_type=SLOType.FRESHNESS_LAG,
                    value=freshness_lag_minutes,
                    target=target,
                    is_met=is_met,
                    error_budget_consumed=error_budget_consumed,
                    additional_data={"article_publish_time": article_publish_time.isoformat(), "discovery_time": discovery_time.isoformat()}
                )
                
                self._record_slo_metric(metric, discovery_time - timedelta(minutes=1), discovery_time)
                self._check_slo_alerts(metric)
                
        except Exception as e:
            logger.error("freshness_lag_slo_record_failed", error=str(e))
    
    def record_response_time(self, response_time_seconds: float, operation: str = "generic"):
        """Record response time measurement."""
        try:
            with self._lock:
                target = self.slo_targets[SLOType.RESPONSE_TIME]
                is_met = response_time_seconds <= target
                error_budget_consumed = max(0, (response_time_seconds - target) / target) if not is_met else 0.0
                
                timestamp = datetime.utcnow()
                metric = SLOMetric(
                    timestamp=timestamp,
                    slo_type=SLOType.RESPONSE_TIME,
                    value=response_time_seconds,
                    target=target,
                    is_met=is_met,
                    error_budget_consumed=error_budget_consumed,
                    additional_data={"operation": operation}
                )
                
                self._record_slo_metric(metric, timestamp - timedelta(seconds=1), timestamp)
                self._check_slo_alerts(metric)
                
        except Exception as e:
            logger.error("response_time_slo_record_failed", error=str(e))
    
    def record_error_rate(self, errors: int, total_requests: int, window_hours: int = 1):
        """Record error rate measurement."""
        try:
            with self._lock:
                if total_requests == 0:
                    return
                
                window_end = datetime.utcnow()
                window_start = window_end - timedelta(hours=window_hours)
                
                error_rate = errors / total_requests
                target = self.slo_targets[SLOType.ERROR_RATE]
                is_met = error_rate <= target
                error_budget_consumed = max(0, (error_rate - target) / target) if not is_met else 0.0
                
                metric = SLOMetric(
                    timestamp=window_end,
                    slo_type=SLOType.ERROR_RATE,
                    value=error_rate,
                    target=target,
                    is_met=is_met,
                    error_budget_consumed=error_budget_consumed,
                    additional_data={"errors": errors, "total_requests": total_requests, "window_hours": window_hours}
                )
                
                self._record_slo_metric(metric, window_start, window_end)
                self._check_slo_alerts(metric)
                
        except Exception as e:
            logger.error("error_rate_slo_record_failed", error=str(e))
    
    def _record_slo_metric(self, metric: SLOMetric, window_start: datetime, window_end: datetime):
        """Record SLO metric to database."""
        try:
            import json
            
            with sqlite3.connect(self.slo_db_path) as conn:
                conn.execute("""
                    INSERT INTO slo_measurements
                    (timestamp, slo_type, measured_value, target_value, is_met, error_budget_consumed,
                     window_start, window_end, additional_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    metric.timestamp.isoformat(),
                    metric.slo_type.value,
                    metric.value,
                    metric.target,
                    metric.is_met,
                    metric.error_budget_consumed,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    json.dumps(metric.additional_data) if metric.additional_data else None
                ))
                
                # Update error budget status
                self._update_error_budget_status(metric.slo_type, window_end)
                
        except Exception as e:
            logger.error("slo_metric_record_failed", slo_type=metric.slo_type.value, error=str(e))
    
    def _update_error_budget_status(self, slo_type: SLOType, timestamp: datetime):
        """Update error budget status for given SLO type."""
        try:
            window_start = timestamp - timedelta(days=self.error_budget_window_days)
            
            with sqlite3.connect(self.slo_db_path) as conn:
                # Calculate error budget consumption over window
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_measurements,
                        COUNT(CASE WHEN is_met = 0 THEN 1 END) as failed_measurements,
                        AVG(error_budget_consumed) as avg_budget_consumed
                    FROM slo_measurements
                    WHERE slo_type = ? AND timestamp >= ?
                """, (slo_type.value, window_start.isoformat()))
                
                result = cursor.fetchone()
                if result and result[0] > 0:
                    total_measurements = result[0]
                    failed_measurements = result[1] or 0
                    
                    # Calculate budget consumed as percentage
                    budget_consumed_percent = (failed_measurements / total_measurements) * 100
                    budget_remaining_percent = max(0, 100 - budget_consumed_percent)
                    
                    # Insert budget status
                    conn.execute("""
                        INSERT INTO error_budget_status
                        (timestamp, slo_type, budget_consumed_percent, budget_remaining_percent,
                         window_start, window_end, total_measurements, failed_measurements)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        timestamp.isoformat(),
                        slo_type.value,
                        budget_consumed_percent,
                        budget_remaining_percent,
                        window_start.isoformat(),
                        timestamp.isoformat(),
                        total_measurements,
                        failed_measurements
                    ))
                    
        except Exception as e:
            logger.error("error_budget_update_failed", slo_type=slo_type.value, error=str(e))
    
    def _check_slo_alerts(self, metric: SLOMetric):
        """Check if SLO alerts should be triggered."""
        try:
            # Check immediate SLO violation
            if not metric.is_met:
                self._trigger_alert(
                    slo_type=metric.slo_type,
                    alert_type="slo_violation",
                    severity="warning",
                    message=f"SLO violation: {metric.slo_type.value} = {metric.value:.4f}, target = {metric.target:.4f}",
                    measured_value=metric.value,
                    target_value=metric.target,
                    error_budget_consumed=metric.error_budget_consumed
                )
            
            # Check error budget exhaustion
            if metric.error_budget_consumed >= self.error_budget_alert_threshold:
                self._trigger_alert(
                    slo_type=metric.slo_type,
                    alert_type="error_budget_exhaustion",
                    severity="critical",
                    message=f"Error budget {metric.error_budget_consumed*100:.1f}% consumed for {metric.slo_type.value}",
                    measured_value=metric.value,
                    target_value=metric.target,
                    error_budget_consumed=metric.error_budget_consumed
                )
            
        except Exception as e:
            logger.error("slo_alert_check_failed", error=str(e))
    
    def _trigger_alert(self, slo_type: SLOType, alert_type: str, severity: str, message: str, 
                      measured_value: float = None, target_value: float = None, error_budget_consumed: float = None):
        """Trigger SLO alert and record it."""
        try:
            timestamp = datetime.utcnow()
            
            # Log alert
            logger.warning("slo_alert_triggered",
                          slo_type=slo_type.value,
                          alert_type=alert_type,
                          severity=severity,
                          message=message,
                          measured_value=measured_value,
                          target_value=target_value)
            
            # Record alert in database
            with sqlite3.connect(self.slo_db_path) as conn:
                conn.execute("""
                    INSERT INTO slo_alerts
                    (timestamp, slo_type, alert_type, severity, message, measured_value, target_value, error_budget_consumed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    timestamp.isoformat(),
                    slo_type.value,
                    alert_type,
                    severity,
                    message,
                    measured_value,
                    target_value,
                    error_budget_consumed
                ))
            
            # TODO: Send alert to external systems (Slack, email, etc.)
            # This could be extended to integrate with alerting systems
            
        except Exception as e:
            logger.error("slo_alert_trigger_failed", error=str(e))
    
    def _calculate_recent_success_rate(self, window_start: datetime, window_end: datetime) -> float:
        """Calculate recent parsing success rate from main database."""
        try:
            if not self.sqlite_manager:
                return 1.0  # Default to success if no database connection
            
            with sqlite3.connect(self.sqlite_manager.db_path) as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(CASE WHEN status = 'completed' THEN 1 END) as successful
                    FROM articles
                    WHERE created_at >= ? AND created_at <= ?
                    AND did_use_micro_ai = 0 AND did_escalate_heavy = 0
                """, (window_start.isoformat(), window_end.isoformat()))
                
                result = cursor.fetchone()
                if result and result[0] > 0:
                    return result[1] / result[0]
                else:
                    return 1.0  # Default to success if no data
                    
        except Exception as e:
            logger.debug("recent_success_rate_calculation_failed", error=str(e))
            return 1.0
    
    def get_slo_status(self, hours: int = 24) -> Dict[str, Any]:
        """Get current SLO status for all tracked objectives."""
        try:
            window_end = datetime.utcnow()
            window_start = window_end - timedelta(hours=hours)
            
            status = {}
            
            with sqlite3.connect(self.slo_db_path) as conn:
                for slo_type in SLOType:
                    cursor = conn.execute("""
                        SELECT 
                            AVG(measured_value) as avg_value,
                            MIN(measured_value) as min_value,
                            MAX(measured_value) as max_value,
                            COUNT(*) as total_measurements,
                            COUNT(CASE WHEN is_met = 1 THEN 1 END) as successful_measurements,
                            AVG(error_budget_consumed) as avg_error_budget_consumed,
                            MAX(timestamp) as latest_measurement
                        FROM slo_measurements
                        WHERE slo_type = ? AND timestamp >= ?
                    """, (slo_type.value, window_start.isoformat()))
                    
                    result = cursor.fetchone()
                    if result and result[6]:  # Has measurements
                        success_rate = result[4] / result[3] if result[3] > 0 else 0.0
                        
                        status[slo_type.value] = {
                            "target": self.slo_targets[slo_type],
                            "current_value": result[0],
                            "min_value": result[1],
                            "max_value": result[2],
                            "success_rate": success_rate,
                            "total_measurements": result[3],
                            "successful_measurements": result[4],
                            "avg_error_budget_consumed": result[5] or 0,
                            "latest_measurement": result[6],
                            "is_meeting_slo": success_rate >= 0.95,  # 95% of measurements should meet SLO
                            "window_hours": hours
                        }
                    else:
                        status[slo_type.value] = {
                            "target": self.slo_targets[slo_type],
                            "current_value": None,
                            "message": "No measurements in time window",
                            "window_hours": hours
                        }
            
            return status
            
        except Exception as e:
            logger.error("slo_status_retrieval_failed", error=str(e))
            return {"error": str(e)}
    
    def get_error_budget_status(self, days: int = None) -> Dict[str, Any]:
        """Get error budget status for all SLO types."""
        days = days or self.error_budget_window_days
        
        try:
            window_end = datetime.utcnow()
            window_start = window_end - timedelta(days=days)
            
            budget_status = {}
            
            with sqlite3.connect(self.slo_db_path) as conn:
                for slo_type in SLOType:
                    cursor = conn.execute("""
                        SELECT 
                            budget_consumed_percent,
                            budget_remaining_percent,
                            total_measurements,
                            failed_measurements,
                            timestamp
                        FROM error_budget_status
                        WHERE slo_type = ? AND timestamp >= ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    """, (slo_type.value, window_start.isoformat()))
                    
                    result = cursor.fetchone()
                    if result:
                        budget_status[slo_type.value] = {
                            "budget_consumed_percent": result[0],
                            "budget_remaining_percent": result[1],
                            "total_measurements": result[2],
                            "failed_measurements": result[3],
                            "last_updated": result[4],
                            "alert_threshold_percent": self.error_budget_alert_threshold * 100,
                            "is_critical": result[0] >= (self.error_budget_alert_threshold * 100),
                            "window_days": days
                        }
                    else:
                        budget_status[slo_type.value] = {
                            "message": "No error budget data available",
                            "window_days": days
                        }
            
            return budget_status
            
        except Exception as e:
            logger.error("error_budget_status_retrieval_failed", error=str(e))
            return {"error": str(e)}
    
    def get_recent_alerts(self, hours: int = 24, severity: str = None) -> List[Dict[str, Any]]:
        """Get recent SLO alerts."""
        try:
            window_start = datetime.utcnow() - timedelta(hours=hours)
            
            with sqlite3.connect(self.slo_db_path) as conn:
                query = """
                    SELECT timestamp, slo_type, alert_type, severity, message, 
                           measured_value, target_value, error_budget_consumed
                    FROM slo_alerts
                    WHERE timestamp >= ?
                """
                params = [window_start.isoformat()]
                
                if severity:
                    query += " AND severity = ?"
                    params.append(severity)
                
                query += " ORDER BY timestamp DESC LIMIT 100"
                
                cursor = conn.execute(query, params)
                alerts = []
                
                for row in cursor.fetchall():
                    alerts.append({
                        "timestamp": row[0],
                        "slo_type": row[1],
                        "alert_type": row[2],
                        "severity": row[3],
                        "message": row[4],
                        "measured_value": row[5],
                        "target_value": row[6],
                        "error_budget_consumed": row[7]
                    })
                
                return alerts
                
        except Exception as e:
            logger.error("recent_alerts_retrieval_failed", error=str(e))
            return []
    
    def cleanup_old_data(self, days_old: int = 90) -> Dict[str, int]:
        """Clean up old SLO data beyond retention period."""
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_old)
            cleanup_results = {}
            
            with sqlite3.connect(self.slo_db_path) as conn:
                # Clean up old measurements
                cursor = conn.execute("""
                    DELETE FROM slo_measurements
                    WHERE timestamp < ?
                """, (cutoff_date.isoformat(),))
                cleanup_results["measurements_deleted"] = cursor.rowcount
                
                # Clean up old error budget status
                cursor = conn.execute("""
                    DELETE FROM error_budget_status
                    WHERE timestamp < ?
                """, (cutoff_date.isoformat(),))
                cleanup_results["budget_status_deleted"] = cursor.rowcount
                
                # Clean up old alerts (keep longer retention for alerts)
                alert_cutoff = datetime.utcnow() - timedelta(days=days_old * 2)
                cursor = conn.execute("""
                    DELETE FROM slo_alerts
                    WHERE timestamp < ?
                """, (alert_cutoff.isoformat(),))
                cleanup_results["alerts_deleted"] = cursor.rowcount
                
                # Vacuum database
                conn.execute("VACUUM")
            
            logger.info("slo_data_cleanup_completed", **cleanup_results)
            return cleanup_results
            
        except Exception as e:
            logger.error("slo_data_cleanup_failed", error=str(e))
            return {"error": str(e)}