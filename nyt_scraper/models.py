"""Data models for NYT Scraper."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum


class ParseMethod(Enum):
    """Parsing methods used to extract article."""
    JSON_LD = "json_ld"
    META_TAGS = "meta_tags"
    AMP = "amp"
    FRAMEWORK = "framework"
    HTML = "html"
    XPATH = "xpath"
    AI_FALLBACK = "ai_fallback"
    HEADLESS = "headless"


class ArticleStatus(Enum):
    """Article processing status."""
    DISCOVERED = "discovered"
    FETCHING = "fetching"
    PARSING = "parsing"
    STORED = "stored"
    FAILED = "failed"
    PAYWALL = "paywall"
    RATE_LIMITED = "rate_limited"


@dataclass
class Article:
    """Represents a NYT article."""
    
    # Required fields
    source_url: str
    title: str
    body: str
    
    # Optional metadata
    canonical_url: Optional[str] = None
    author: Optional[str] = None
    published_date: Optional[datetime] = None
    updated_date: Optional[datetime] = None
    section: Optional[str] = None
    summary: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    
    # Technical metadata
    redirect_chain: List[str] = field(default_factory=list)
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    body_hash: Optional[str] = None
    simhash: Optional[int] = None
    
    # Processing metadata
    parse_method: Optional[ParseMethod] = None
    parse_duration_ms: Optional[float] = None
    fetch_duration_ms: Optional[float] = None
    status: ArticleStatus = ArticleStatus.DISCOVERED
    error_message: Optional[str] = None
    
    # Enhanced telemetry metadata
    parser_path: Optional[str] = None  # Which parser succeeded (e.g., "json_ld->meta_tags")
    parse_confidence: Optional[float] = None  # Confidence score (0-1)
    did_use_micro_ai: bool = False  # Used micro-AI gap fill
    did_escalate_heavy: bool = False  # Escalated to heavy AI
    challenge_detected: bool = False  # Bot challenge detected
    paywall_detected: bool = False  # Paywall detected
    word_count: Optional[int] = None  # Content word count
    char_count: Optional[int] = None  # Content character count
    normalized_checksum: Optional[str] = None  # Content checksum for validation
    
    # Timestamps
    discovered_at: datetime = field(default_factory=datetime.utcnow)
    fetched_at: Optional[datetime] = None
    parsed_at: Optional[datetime] = None
    stored_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        """Convert article to dictionary for storage."""
        return {
            "source_url": self.source_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "author": self.author,
            "published_date": self.published_date.isoformat() if self.published_date else None,
            "updated_date": self.updated_date.isoformat() if self.updated_date else None,
            "body": self.body,
            "section": self.section,
            "summary": self.summary,
            "tags": ",".join(self.tags) if self.tags else None,
            "redirect_chain": str(self.redirect_chain) if self.redirect_chain else None,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "body_hash": self.body_hash,
            "simhash": self.simhash,
            "parse_method": self.parse_method.value if self.parse_method else None,
            "parse_duration_ms": self.parse_duration_ms,
            "fetch_duration_ms": self.fetch_duration_ms,
            "status": self.status.value,
            "error_message": self.error_message,
            # Enhanced telemetry fields
            "parser_path": self.parser_path,
            "parse_confidence": self.parse_confidence,
            "did_use_micro_ai": self.did_use_micro_ai,
            "did_escalate_heavy": self.did_escalate_heavy,
            "challenge_detected": self.challenge_detected,
            "paywall_detected": self.paywall_detected,
            "word_count": self.word_count,
            "char_count": self.char_count,
            "normalized_checksum": self.normalized_checksum,
            # Timestamps
            "discovered_at": self.discovered_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "parsed_at": self.parsed_at.isoformat() if self.parsed_at else None,
            "stored_at": self.stored_at.isoformat() if self.stored_at else None,
        }
    
    def is_valid(self) -> bool:
        """Check if article meets minimum requirements."""
        if not self.title or not self.body:
            return False
        
        # Check minimum content requirements
        word_count = len(self.body.split())
        char_count = len(self.body)
        paragraph_count = self.body.count('\n\n') + 1
        
        # Exception for briefs
        if self.section and "brief" in self.section.lower():
            return word_count >= 50
        
        # Standard requirements
        return (
            char_count >= 2000 or 
            paragraph_count >= 5 or 
            word_count >= 300
        )


@dataclass
class FetchResult:
    """Result of fetching a URL."""
    
    url: str
    status_code: int
    content: Optional[bytes] = None
    headers: Dict[str, str] = field(default_factory=dict)
    redirect_chain: List[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: Optional[str] = None
    from_cache: bool = False
    
    @property
    def is_success(self) -> bool:
        """Check if fetch was successful."""
        return 200 <= self.status_code < 300 and self.content is not None
    
    @property
    def is_rate_limited(self) -> bool:
        """Check if we hit rate limiting."""
        return self.status_code in (429, 403)
    
    @property
    def is_paywall(self) -> bool:
        """Check if we hit a paywall (451 status)."""
        return self.status_code == 451


@dataclass
class ParseResult:
    """Result of parsing HTML content."""
    
    success: bool
    article: Optional[Article] = None
    parse_method: Optional[ParseMethod] = None
    duration_ms: float = 0.0
    error: Optional[str] = None
    confidence: float = 0.0
    
    @property
    def needs_fallback(self) -> bool:
        """Check if we should try another parser."""
        return not self.success or self.confidence < 0.5