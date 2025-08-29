"""Tests for SLO (Service Level Objectives) tracking and monitoring."""

import pytest
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from nyt_scraper.monitoring.slo_tracker import SLOTracker, SLOType, SLOMetric
from nyt_scraper.config import config


class TestSLOTracker:
    """Test SLO tracking functionality."""
    
    @pytest.fixture
    def temp_slo_db(self):
        """Create temporary SLO database."""
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp_file.close()
        db_path = Path(temp_file.name)
        yield db_path
        
        # Windows-compatible cleanup with retry logic
        if db_path.exists():
            import time
            import gc
            
            # Force garbage collection to release file handles
            gc.collect()
            time.sleep(0.1)
            
            # Retry deletion up to 3 times
            for attempt in range(3):
                try:
                    db_path.unlink()
                    break
                except PermissionError:
                    if attempt < 2:  # Try 2 more times
                        time.sleep(0.3)
                        gc.collect()
                        continue
                    # Final attempt failed - ignore on Windows
                    pass
    
    @pytest.fixture
    def slo_tracker(self, temp_slo_db):
        """Create SLO tracker with temporary database."""
        with patch('nyt_scraper.monitoring.slo_tracker.config') as mock_config:
            mock_config.data_dir = temp_slo_db.parent
            mock_config.slo_deterministic_success_rate = 0.95
            mock_config.slo_ai_usage_rate = 0.01
            mock_config.slo_freshness_lag_minutes = 60
            
            tracker = SLOTracker()
            tracker.slo_db_path = temp_slo_db
            tracker._init_slo_db()
            
            return tracker
    
    def test_slo_database_initialization(self, slo_tracker):
        """Test SLO database schema creation."""
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            # Check tables exist
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name IN ('slo_measurements', 'error_budget_status', 'slo_alerts')
            """)
            tables = {row[0] for row in cursor.fetchall()}
            
        expected_tables = {'slo_measurements', 'error_budget_status', 'slo_alerts'}
        assert expected_tables.issubset(tables)
    
    def test_deterministic_parsing_slo_recording(self, slo_tracker):
        """Test recording deterministic parsing success rate."""
        # Record successful parsing
        slo_tracker.record_deterministic_parsing_result(success=True, total_attempts=100)
        
        # Explicitly flush to ensure data is written
        slo_tracker.flush()
        
        # Verify recording
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT measured_value, target_value, is_met 
                FROM slo_measurements 
                WHERE slo_type = ?
            """, (SLOType.DETERMINISTIC_SUCCESS_RATE.value,))
            
            result = cursor.fetchone()
            assert result is not None
            assert result[0] >= 0.95  # Success rate should meet target
            assert result[1] == 0.95  # Target value
            assert result[2] == 1     # is_met = True
    
    def test_ai_usage_slo_recording(self, slo_tracker):
        """Test recording AI usage rate SLO."""
        # Record AI usage within limits
        slo_tracker.record_ai_usage(ai_requests=5, total_requests=1000)
        slo_tracker.flush()
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT measured_value, is_met 
                FROM slo_measurements 
                WHERE slo_type = ?
            """, (SLOType.AI_USAGE_RATE.value,))
            
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == 0.005  # 5/1000 = 0.5%
            assert result[1] == 1      # is_met = True (under 1% target)
    
    def test_ai_usage_slo_violation(self, slo_tracker):
        """Test AI usage SLO violation detection."""
        # Record AI usage over limits
        slo_tracker.record_ai_usage(ai_requests=25, total_requests=1000)
        slo_tracker.flush()
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT measured_value, is_met, error_budget_consumed
                FROM slo_measurements 
                WHERE slo_type = ?
            """, (SLOType.AI_USAGE_RATE.value,))
            
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == 0.025  # 25/1000 = 2.5%
            assert result[1] == 0      # is_met = False (over 1% target)
            assert result[2] > 0       # error_budget_consumed > 0
    
    def test_freshness_lag_slo_recording(self, slo_tracker):
        """Test freshness lag SLO recording."""
        publish_time = datetime.utcnow() - timedelta(minutes=30)
        discovery_time = datetime.utcnow()
        
        slo_tracker.record_freshness_lag(publish_time, discovery_time)
        slo_tracker.flush()
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT measured_value, is_met 
                FROM slo_measurements 
                WHERE slo_type = ?
            """, (SLOType.FRESHNESS_LAG.value,))
            
            result = cursor.fetchone()
            assert result is not None
            assert 25 <= result[0] <= 35  # ~30 minutes lag
            assert result[1] == 1         # is_met = True (under 60 min target)
    
    def test_response_time_slo_recording(self, slo_tracker):
        """Test response time SLO recording."""
        # Record fast response time
        slo_tracker.record_response_time(15.5, "article_parsing")
        slo_tracker.flush()
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT measured_value, is_met, additional_data
                FROM slo_measurements 
                WHERE slo_type = ?
            """, (SLOType.RESPONSE_TIME.value,))
            
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == 15.5  # Response time
            assert result[1] == 1     # is_met = True (under 30s target)
            assert "article_parsing" in result[2]  # operation tracked
    
    def test_error_rate_slo_recording(self, slo_tracker):
        """Test error rate SLO recording."""
        # Record error rate within acceptable limits
        slo_tracker.record_error_rate(errors=25, total_requests=1000)
        slo_tracker.flush()
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT measured_value, is_met
                FROM slo_measurements 
                WHERE slo_type = ?
            """, (SLOType.ERROR_RATE.value,))
            
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == 0.025  # 2.5% error rate
            assert result[1] == 1      # is_met = True (under 5% target)


class TestErrorBudgetTracking:
    """Test error budget calculation and tracking."""
    
    @pytest.fixture
    def slo_tracker_with_data(self, slo_tracker):
        """Create SLO tracker with test data."""
        # Create mixed success/failure data over time
        base_time = datetime.utcnow() - timedelta(days=7)
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            for day in range(7):
                for hour in range(24):
                    timestamp = base_time + timedelta(days=day, hours=hour)
                    
                    # 90% success rate with some variations
                    success_rate = 0.90 + (0.05 * (hour % 3))  # Vary between 90-95%
                    is_met = success_rate >= 0.95
                    error_budget_consumed = max(0, (0.95 - success_rate) / 0.95) if not is_met else 0
                    
                    conn.execute("""
                        INSERT INTO slo_measurements
                        (timestamp, slo_type, measured_value, target_value, is_met, error_budget_consumed,
                         window_start, window_end)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        timestamp.isoformat(),
                        SLOType.DETERMINISTIC_SUCCESS_RATE.value,
                        success_rate,
                        0.95,
                        is_met,
                        error_budget_consumed,
                        timestamp.isoformat(),
                        timestamp.isoformat()
                    ))
            
            conn.commit()
        
        return slo_tracker
    
    def test_error_budget_calculation(self, slo_tracker_with_data):
        """Test error budget status calculation."""
        # Update error budget for recent data
        slo_tracker_with_data._update_error_budget_status(
            SLOType.DETERMINISTIC_SUCCESS_RATE, 
            datetime.utcnow()
        )
        
        # Get error budget status
        budget_status = slo_tracker_with_data.get_error_budget_status(days=7)
        
        assert SLOType.DETERMINISTIC_SUCCESS_RATE.value in budget_status
        status = budget_status[SLOType.DETERMINISTIC_SUCCESS_RATE.value]
        
        assert "budget_consumed_percent" in status
        assert "budget_remaining_percent" in status
        assert "total_measurements" in status
        assert status["total_measurements"] > 0
        assert 0 <= status["budget_consumed_percent"] <= 100
        assert status["budget_remaining_percent"] >= 0
    
    def test_error_budget_alerts(self, slo_tracker):
        """Test error budget alert triggering."""
        # Record measurements that should trigger alerts
        for _ in range(10):
            # High error budget consumption
            slo_tracker.record_deterministic_parsing_result(success=False, total_attempts=1)
        
        # Check for alerts
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("""
                SELECT alert_type, severity, message 
                FROM slo_alerts 
                WHERE slo_type = ?
            """, (SLOType.DETERMINISTIC_SUCCESS_RATE.value,))
            
            alerts = cursor.fetchall()
            
        # Should have SLO violation alerts
        assert len(alerts) > 0
        alert_types = {alert[0] for alert in alerts}
        assert "slo_violation" in alert_types


class TestSLOStatusReporting:
    """Test SLO status reporting and monitoring."""
    
    @pytest.fixture
    def slo_tracker_recent_data(self, slo_tracker):
        """SLO tracker with recent measurement data."""
        # Add recent measurements
        now = datetime.utcnow()
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            # Add various SLO measurements
            measurements = [
                (SLOType.DETERMINISTIC_SUCCESS_RATE.value, 0.96, 0.95, True),
                (SLOType.AI_USAGE_RATE.value, 0.008, 0.01, True),
                (SLOType.FRESHNESS_LAG.value, 45.0, 60.0, True),
                (SLOType.RESPONSE_TIME.value, 25.0, 30.0, True),
                (SLOType.ERROR_RATE.value, 0.03, 0.05, True),
            ]
            
            for slo_type, measured, target, is_met in measurements:
                for i in range(5):  # Multiple measurements
                    timestamp = now - timedelta(minutes=i * 10)
                    conn.execute("""
                        INSERT INTO slo_measurements
                        (timestamp, slo_type, measured_value, target_value, is_met, error_budget_consumed)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (timestamp.isoformat(), slo_type, measured, target, is_met, 0.0))
            
            conn.commit()
        
        return slo_tracker
    
    def test_slo_status_retrieval(self, slo_tracker_recent_data):
        """Test SLO status retrieval."""
        status = slo_tracker_recent_data.get_slo_status(hours=1)
        
        # Check all SLO types are reported
        expected_slos = [
            SLOType.DETERMINISTIC_SUCCESS_RATE.value,
            SLOType.AI_USAGE_RATE.value,
            SLOType.FRESHNESS_LAG.value,
            SLOType.RESPONSE_TIME.value,
            SLOType.ERROR_RATE.value
        ]
        
        for slo_name in expected_slos:
            assert slo_name in status
            slo_status = status[slo_name]
            
            assert "target" in slo_status
            assert "current_value" in slo_status
            assert "success_rate" in slo_status
            assert "total_measurements" in slo_status
    
    def test_recent_alerts_retrieval(self, slo_tracker):
        """Test recent alerts retrieval."""
        # Trigger some alerts by recording SLO violations
        slo_tracker.record_ai_usage(ai_requests=50, total_requests=1000)  # Over limit
        slo_tracker.record_response_time(45.0)  # Over target
        
        # Get recent alerts
        alerts = slo_tracker.get_recent_alerts(hours=1)
        
        assert len(alerts) > 0
        
        for alert in alerts:
            assert "timestamp" in alert
            assert "slo_type" in alert
            assert "alert_type" in alert
            assert "severity" in alert
            assert "message" in alert
    
    def test_alert_severity_filtering(self, slo_tracker):
        """Test filtering alerts by severity."""
        # Create alerts of different severities
        slo_tracker.record_ai_usage(ai_requests=15, total_requests=1000)  # Warning
        slo_tracker.record_error_rate(errors=100, total_requests=1000)    # Critical
        
        # Get only critical alerts
        critical_alerts = slo_tracker.get_recent_alerts(hours=1, severity="critical")
        warning_alerts = slo_tracker.get_recent_alerts(hours=1, severity="warning")
        
        # Should have different counts
        assert len(critical_alerts) != len(warning_alerts)


class TestSLOPerformance:
    """Test SLO tracking performance."""
    
    def test_slo_recording_performance(self, slo_tracker):
        """Test performance of SLO recording operations."""
        import time
        
        # Use smaller batch size for testing
        original_batch_size = slo_tracker._batch_size
        slo_tracker._batch_size = 50  # Smaller batches for tests
        
        try:
            start_time = time.time()
            
            # Record many SLO measurements
            for i in range(1000):
                slo_tracker.record_deterministic_parsing_result(
                    success=(i % 10) != 0,  # 90% success rate
                    total_attempts=1
                )
            
            # Force flush any remaining metrics
            slo_tracker.flush()
            
            duration = time.time() - start_time
            
            # Should complete within reasonable time (much faster now)
            assert duration < 2.0  # 2 seconds for 1000 recordings with batching
            
            # Verify all measurements were recorded
            with sqlite3.connect(slo_tracker.slo_db_path) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM slo_measurements")
                count = cursor.fetchone()[0]
                assert count >= 1000
                
        finally:
            # Restore original batch size
            slo_tracker._batch_size = original_batch_size
    
    def test_slo_query_performance(self, slo_tracker):
        """Test performance of SLO status queries."""
        import time
        
        # Add substantial data
        now = datetime.utcnow()
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            for i in range(10000):
                timestamp = now - timedelta(minutes=i)
                conn.execute("""
                    INSERT INTO slo_measurements
                    (timestamp, slo_type, measured_value, target_value, is_met, error_budget_consumed)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    timestamp.isoformat(),
                    SLOType.DETERMINISTIC_SUCCESS_RATE.value,
                    0.95,
                    0.95,
                    True,
                    0.0
                ))
            conn.commit()
        
        # Test query performance
        start_time = time.time()
        status = slo_tracker.get_slo_status(hours=24)
        duration = time.time() - start_time
        
        assert duration < 2.0  # Should complete within 2 seconds
        assert len(status) > 0


class TestSLODataCleanup:
    """Test SLO data cleanup and maintenance."""
    
    def test_old_data_cleanup(self, slo_tracker):
        """Test cleanup of old SLO data."""
        # Add old data
        old_time = datetime.utcnow() - timedelta(days=100)
        recent_time = datetime.utcnow() - timedelta(days=1)
        
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            # Old measurements
            for i in range(50):
                conn.execute("""
                    INSERT INTO slo_measurements
                    (timestamp, slo_type, measured_value, target_value, is_met, error_budget_consumed)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    old_time.isoformat(),
                    SLOType.DETERMINISTIC_SUCCESS_RATE.value,
                    0.95, 0.95, True, 0.0
                ))
            
            # Recent measurements
            for i in range(50):
                conn.execute("""
                    INSERT INTO slo_measurements
                    (timestamp, slo_type, measured_value, target_value, is_met, error_budget_consumed)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    recent_time.isoformat(),
                    SLOType.DETERMINISTIC_SUCCESS_RATE.value,
                    0.95, 0.95, True, 0.0
                ))
            
            conn.commit()
        
        # Run cleanup
        cleanup_results = slo_tracker.cleanup_old_data(days_old=90)
        
        assert "measurements_deleted" in cleanup_results
        assert cleanup_results["measurements_deleted"] == 50
        
        # Verify recent data remains
        with sqlite3.connect(slo_tracker.slo_db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM slo_measurements")
            remaining_count = cursor.fetchone()[0]
            assert remaining_count == 50


class TestSLOIntegration:
    """Test SLO integration with scraper components."""
    
    def test_slo_integration_with_scraper_stats(self, slo_tracker):
        """Test SLO integration with scraper statistics."""
        # Simulate scraper statistics
        scraper_stats = {
            'total_processed': 1000,
            'deterministic_success': 950,
            'micro_ai_used': 30,
            'heavy_ai_used': 5,
            'cache_hits': 600,
            'duplicates_found': 20
        }
        
        # Calculate and record SLOs based on scraper stats
        deterministic_rate = scraper_stats['deterministic_success'] / scraper_stats['total_processed']
        ai_usage_rate = (scraper_stats['micro_ai_used'] + scraper_stats['heavy_ai_used']) / scraper_stats['total_processed']
        
        # Record a single deterministic parsing result 
        # This will be recorded as 1.0 since deterministic_rate >= 0.95
        slo_tracker.record_deterministic_parsing_result(
            success=deterministic_rate >= 0.95,
            total_attempts=scraper_stats['total_processed']
        )
        
        slo_tracker.record_ai_usage(
            ai_requests=scraper_stats['micro_ai_used'] + scraper_stats['heavy_ai_used'],
            total_requests=scraper_stats['total_processed']
        )
        
        # Verify SLO recordings
        status = slo_tracker.get_slo_status(hours=1)
        
        assert SLOType.DETERMINISTIC_SUCCESS_RATE.value in status
        assert SLOType.AI_USAGE_RATE.value in status
        
        det_status = status[SLOType.DETERMINISTIC_SUCCESS_RATE.value]
        ai_status = status[SLOType.AI_USAGE_RATE.value]
        
        # The deterministic rate will be recorded as 1.0 since the check passed (0.95 >= 0.95)
        assert det_status['current_value'] == 1.0  # Success was recorded as True
        assert abs(ai_status['current_value'] - ai_usage_rate) < 0.001  # Allow small floating point difference


if __name__ == "__main__":
    pytest.main([__file__, "-v"])