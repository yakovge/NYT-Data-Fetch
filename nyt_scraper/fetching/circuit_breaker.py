"""Circuit breaker for handling rate limits and failures."""

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional

from ..monitoring import MetricsCollector, get_logger

logger = get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitStatus:
    """Status of a circuit breaker."""
    state: CircuitState
    failure_count: int
    last_failure_time: Optional[datetime]
    last_success_time: Optional[datetime]
    cool_off_until: Optional[datetime]
    
    def is_open(self) -> bool:
        """Check if circuit is open."""
        if self.state != CircuitState.OPEN:
            return False
        
        # Check if cool-off period has passed
        if self.cool_off_until and datetime.utcnow() > self.cool_off_until:
            return False
        
        return True


class CircuitBreaker:
    """Per-host circuit breaker for rate limiting protection."""
    
    def __init__(self, metrics: MetricsCollector):
        self.metrics = metrics
        self.circuits: Dict[str, CircuitStatus] = defaultdict(
            lambda: CircuitStatus(
                state=CircuitState.CLOSED,
                failure_count=0,
                last_failure_time=None,
                last_success_time=None,
                cool_off_until=None
            )
        )
        
        # Configuration
        self.failure_threshold = 5  # Failures before opening
        self.recovery_timeout = timedelta(minutes=5)  # Cool-off period
        self.half_open_requests = 3  # Requests to test in half-open
        
        # Rate limit tracking
        self.rate_limit_counts: Dict[str, int] = defaultdict(int)
        self.rate_limit_window = timedelta(minutes=10)
        self.rate_limit_events: Dict[str, list] = defaultdict(list)
    
    def can_request(self, host: str) -> bool:
        """Check if request to host is allowed."""
        circuit = self.circuits[host]
        
        # Circuit is closed - allow
        if circuit.state == CircuitState.CLOSED:
            return True
        
        # Circuit is open - check cool-off
        if circuit.state == CircuitState.OPEN:
            if circuit.cool_off_until and datetime.utcnow() > circuit.cool_off_until:
                # Move to half-open for testing
                circuit.state = CircuitState.HALF_OPEN
                circuit.failure_count = 0
                logger.info("circuit_half_open", host=host)
                return True
            return False
        
        # Circuit is half-open - allow limited requests
        if circuit.state == CircuitState.HALF_OPEN:
            return circuit.failure_count < self.half_open_requests
        
        return False
    
    def record_response(self, host: str, status_code: int):
        """Record HTTP response for circuit breaker logic."""
        circuit = self.circuits[host]
        
        # Success response
        if 200 <= status_code < 300:
            self.record_success(host)
            return
        
        # Rate limiting response
        if status_code in (429, 403):
            self.record_rate_limit(host, status_code)
            return
        
        # Server errors
        if status_code >= 500:
            self.record_failure(host)
    
    def record_success(self, host: str):
        """Record successful request."""
        circuit = self.circuits[host]
        circuit.last_success_time = datetime.utcnow()
        
        # Reset circuit if it was half-open
        if circuit.state == CircuitState.HALF_OPEN:
            circuit.state = CircuitState.CLOSED
            circuit.failure_count = 0
            circuit.cool_off_until = None
            logger.info("circuit_closed", host=host)
    
    def record_failure(self, host: str):
        """Record failed request."""
        circuit = self.circuits[host]
        circuit.failure_count += 1
        circuit.last_failure_time = datetime.utcnow()
        
        # Check if we should open the circuit
        if circuit.failure_count >= self.failure_threshold:
            self.trip_circuit(host, "failure_threshold")
    
    def record_rate_limit(self, host: str, status_code: int):
        """Record rate limiting event."""
        now = datetime.utcnow()
        
        # Clean old events
        self.rate_limit_events[host] = [
            event for event in self.rate_limit_events[host]
            if now - event < self.rate_limit_window
        ]
        
        # Add new event
        self.rate_limit_events[host].append(now)
        self.rate_limit_counts[host] += 1
        
        # Check if we should trip the circuit
        if len(self.rate_limit_events[host]) >= 10:
            self.trip_circuit(host, f"rate_limit_{status_code}")
            
            # Alert on repeated rate limiting
            self.metrics.record_rate_limit(host, status_code)
    
    def trip_circuit(self, host: str, reason: str):
        """Open the circuit breaker."""
        circuit = self.circuits[host]
        circuit.state = CircuitState.OPEN
        circuit.cool_off_until = datetime.utcnow() + self.recovery_timeout
        
        logger.warning(
            "circuit_tripped",
            host=host,
            reason=reason,
            cool_off_until=circuit.cool_off_until.isoformat()
        )
        
        self.metrics.record_circuit_breaker(host, reason)
    
    def reset_circuit(self, host: str):
        """Manually reset a circuit."""
        circuit = self.circuits[host]
        circuit.state = CircuitState.CLOSED
        circuit.failure_count = 0
        circuit.cool_off_until = None
        
        logger.info("circuit_reset", host=host)
    
    def get_status(self, host: str) -> Dict:
        """Get circuit status for monitoring."""
        circuit = self.circuits[host]
        return {
            "host": host,
            "state": circuit.state.value,
            "failure_count": circuit.failure_count,
            "is_open": circuit.is_open(),
            "cool_off_until": (
                circuit.cool_off_until.isoformat()
                if circuit.cool_off_until else None
            ),
            "rate_limit_events": len(self.rate_limit_events[host])
        }
    
    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get all circuit statuses."""
        return {
            host: self.get_status(host)
            for host in self.circuits.keys()
        }