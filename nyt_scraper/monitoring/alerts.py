"""Alert management for critical events."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

import httpx

from ..config import config
from .structured_logger import get_logger

logger = get_logger(__name__)


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertType(Enum):
    """Types of alerts."""
    PARSE_FAILURE_RATE = "parse_failure_rate"
    AI_BUDGET = "ai_budget"
    RATE_LIMIT = "rate_limit"
    CIRCUIT_BREAKER = "circuit_breaker"
    STORAGE_FULL = "storage_full"
    COMPLIANCE_VIOLATION = "compliance_violation"
    SYSTEM_ERROR = "system_error"


@dataclass
class Alert:
    """Represents an alert."""
    type: AlertType
    severity: AlertSeverity
    message: str
    details: Dict
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()
    
    def to_dict(self) -> Dict:
        """Convert alert to dictionary."""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat()
        }


class AlertManager:
    """Manages alerts and notifications."""
    
    def __init__(self):
        self.alerts: List[Alert] = []
        self.alert_counts: Dict[AlertType, int] = {}
        self.last_alert_time: Dict[AlertType, datetime] = {}
        self.alert_cooldown = timedelta(minutes=5)
        
        # Alert thresholds
        self.thresholds = {
            AlertType.PARSE_FAILURE_RATE: 5.0,  # 5% failure rate
            AlertType.AI_BUDGET: 80.0,  # 80% budget used
            AlertType.RATE_LIMIT: 10,  # 10 events
            AlertType.STORAGE_FULL: 90.0,  # 90% full
        }
    
    def send_alert(self, alert: Alert):
        """Send an alert."""
        # Check cooldown
        if not self._should_send_alert(alert):
            logger.debug(
                "alert_suppressed",
                type=alert.type.value,
                reason="cooldown"
            )
            return
        
        # Store alert
        self.alerts.append(alert)
        self.alert_counts[alert.type] = \
            self.alert_counts.get(alert.type, 0) + 1
        self.last_alert_time[alert.type] = alert.timestamp
        
        # Log alert
        logger.warning(
            "alert_triggered",
            **alert.to_dict()
        )
        
        # Send to Slack if configured
        if config.slack_webhook and alert.severity in [
            AlertSeverity.ERROR,
            AlertSeverity.CRITICAL
        ]:
            self._send_to_slack(alert)
    
    def _should_send_alert(self, alert: Alert) -> bool:
        """Check if alert should be sent based on cooldown."""
        if alert.type not in self.last_alert_time:
            return True
        
        time_since_last = alert.timestamp - self.last_alert_time[alert.type]
        return time_since_last > self.alert_cooldown
    
    async def _send_to_slack(self, alert: Alert):
        """Send alert to Slack webhook."""
        if not config.slack_webhook:
            return
        
        # Format message for Slack
        color = {
            AlertSeverity.INFO: "good",
            AlertSeverity.WARNING: "warning",
            AlertSeverity.ERROR: "danger",
            AlertSeverity.CRITICAL: "danger"
        }.get(alert.severity, "warning")
        
        payload = {
            "attachments": [{
                "color": color,
                "title": f"🚨 NYT Scraper Alert: {alert.type.value}",
                "text": alert.message,
                "fields": [
                    {
                        "title": key,
                        "value": str(value),
                        "short": True
                    }
                    for key, value in alert.details.items()
                ],
                "footer": "NYT Scraper",
                "ts": int(alert.timestamp.timestamp())
            }]
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    config.slack_webhook,
                    json=payload,
                    timeout=10.0
                )
                
                if response.status_code != 200:
                    logger.error(
                        "slack_alert_failed",
                        status=response.status_code,
                        response=response.text
                    )
                    
        except Exception as e:
            logger.error(
                "slack_alert_error",
                error=str(e)
            )
    
    def check_parse_failure_rate(self, failure_rate: float):
        """Check if parse failure rate exceeds threshold."""
        threshold = self.thresholds[AlertType.PARSE_FAILURE_RATE]
        
        if failure_rate > threshold:
            alert = Alert(
                type=AlertType.PARSE_FAILURE_RATE,
                severity=AlertSeverity.WARNING,
                message=f"Parse failure rate {failure_rate:.1f}% exceeds threshold {threshold}%",
                details={
                    "failure_rate": failure_rate,
                    "threshold": threshold
                }
            )
            self.send_alert(alert)
    
    def check_ai_budget(self, usage_percent: float):
        """Check if AI budget usage is high."""
        threshold = self.thresholds[AlertType.AI_BUDGET]
        
        if usage_percent > threshold:
            severity = (
                AlertSeverity.CRITICAL if usage_percent > 95
                else AlertSeverity.WARNING
            )
            
            alert = Alert(
                type=AlertType.AI_BUDGET,
                severity=severity,
                message=f"AI budget usage at {usage_percent:.1f}%",
                details={
                    "usage_percent": usage_percent,
                    "threshold": threshold
                }
            )
            self.send_alert(alert)
    
    def alert_rate_limit(self, host: str, count: int):
        """Alert on repeated rate limiting."""
        threshold = self.thresholds[AlertType.RATE_LIMIT]
        
        if count > threshold:
            alert = Alert(
                type=AlertType.RATE_LIMIT,
                severity=AlertSeverity.ERROR,
                message=f"Repeated rate limiting from {host}",
                details={
                    "host": host,
                    "count": count,
                    "threshold": threshold
                }
            )
            self.send_alert(alert)
    
    def alert_circuit_breaker(self, host: str, reason: str):
        """Alert when circuit breaker trips."""
        alert = Alert(
            type=AlertType.CIRCUIT_BREAKER,
            severity=AlertSeverity.WARNING,
            message=f"Circuit breaker tripped for {host}",
            details={
                "host": host,
                "reason": reason
            }
        )
        self.send_alert(alert)
    
    def alert_storage_full(self, usage_percent: float, size_gb: float):
        """Alert when storage is nearly full."""
        threshold = self.thresholds[AlertType.STORAGE_FULL]
        
        if usage_percent > threshold:
            alert = Alert(
                type=AlertType.STORAGE_FULL,
                severity=AlertSeverity.WARNING,
                message=f"Storage at {usage_percent:.1f}% capacity",
                details={
                    "usage_percent": usage_percent,
                    "size_gb": size_gb,
                    "threshold": threshold
                }
            )
            self.send_alert(alert)
    
    def alert_compliance_violation(self, violation_type: str, details: Dict):
        """Alert on compliance violations."""
        alert = Alert(
            type=AlertType.COMPLIANCE_VIOLATION,
            severity=AlertSeverity.CRITICAL,
            message=f"Compliance violation: {violation_type}",
            details=details
        )
        self.send_alert(alert)
    
    def get_recent_alerts(self, hours: int = 24) -> List[Alert]:
        """Get alerts from the last N hours."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return [
            alert for alert in self.alerts
            if alert.timestamp > cutoff
        ]
    
    def get_alert_summary(self) -> Dict:
        """Get summary of alerts."""
        recent_alerts = self.get_recent_alerts(24)
        
        severity_counts = {}
        for alert in recent_alerts:
            severity = alert.severity.value
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        
        return {
            "total_alerts_24h": len(recent_alerts),
            "by_severity": severity_counts,
            "by_type": {
                type.value: count 
                for type, count in self.alert_counts.items()
            },
            "recent_critical": [
                alert.to_dict() for alert in recent_alerts[-5:]
                if alert.severity == AlertSeverity.CRITICAL
            ]
        }