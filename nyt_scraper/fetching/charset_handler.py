"""Character encoding detection and normalization."""

import re
from typing import Optional, Tuple

import chardet
from selectolax.parser import HTMLParser

from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


class CharsetHandler:
    """Handle character encoding detection and normalization."""
    
    def __init__(self):
        self.confidence_threshold = 0.7
        self.default_encoding = "utf-8"
    
    def normalize_encoding(
        self, 
        content: bytes, 
        content_type: Optional[str] = None
    ) -> bytes:
        """
        Normalize content encoding to UTF-8.
        
        Priority:
        1. <meta charset> tag
        2. Content-Type header
        3. chardet detection (>70% confidence)
        4. UTF-8 fallback
        """
        # First, try to get encoding from content
        encoding = self._detect_encoding(content, content_type)
        
        if encoding and encoding.lower() != "utf-8":
            try:
                # Decode with detected encoding and re-encode as UTF-8
                text = content.decode(encoding, errors="ignore")
                content = text.encode("utf-8")
                logger.debug(
                    "encoding_normalized",
                    from_encoding=encoding,
                    to_encoding="utf-8"
                )
            except Exception as e:
                logger.warning(
                    "encoding_normalization_failed",
                    encoding=encoding,
                    error=str(e)
                )
        
        return content
    
    def _detect_encoding(
        self, 
        content: bytes, 
        content_type: Optional[str]
    ) -> Optional[str]:
        """Detect content encoding using multiple methods."""
        
        # Try to parse HTML and look for meta charset
        encoding = self._get_meta_charset(content)
        if encoding:
            logger.debug("encoding_from_meta", encoding=encoding)
            return encoding
        
        # Try Content-Type header
        if content_type:
            encoding = self._parse_content_type(content_type)
            if encoding:
                logger.debug("encoding_from_header", encoding=encoding)
                return encoding
        
        # Try chardet detection
        detection = chardet.detect(content)
        if detection["confidence"] >= self.confidence_threshold:
            encoding = detection["encoding"]
            logger.debug(
                "encoding_from_chardet",
                encoding=encoding,
                confidence=detection["confidence"]
            )
            return encoding
        
        # Default to UTF-8
        logger.debug("encoding_default", encoding=self.default_encoding)
        return self.default_encoding
    
    def _get_meta_charset(self, content: bytes) -> Optional[str]:
        """Extract charset from HTML meta tags."""
        try:
            # Try to parse as UTF-8 first to find meta tags
            html = content.decode("utf-8", errors="ignore")[:5000]  # Only check beginning
            
            # Look for HTML5 meta charset
            match = re.search(
                r'<meta\s+charset=["\']*([^"\'>]+)',
                html,
                re.IGNORECASE
            )
            if match:
                return match.group(1).strip()
            
            # Look for HTML4 meta http-equiv
            match = re.search(
                r'<meta\s+http-equiv=["\']*content-type["\']*\s+content=["\']*([^"\'>]+)',
                html,
                re.IGNORECASE
            )
            if match:
                content_type = match.group(1)
                return self._parse_content_type(content_type)
            
        except Exception as e:
            logger.debug("meta_charset_extraction_failed", error=str(e))
        
        return None
    
    def _parse_content_type(self, content_type: str) -> Optional[str]:
        """Extract charset from Content-Type header."""
        if not content_type:
            return None
        
        # Look for charset parameter
        match = re.search(r'charset=([^;,\s]+)', content_type, re.IGNORECASE)
        if match:
            charset = match.group(1).strip('"\'')
            # Normalize common misspellings
            charset_map = {
                "utf8": "utf-8",
                "iso-8859-1": "latin-1",
                "iso8859-1": "latin-1",
                "windows-1252": "cp1252"
            }
            return charset_map.get(charset.lower(), charset)
        
        return None
    
    def validate_utf8(self, content: bytes) -> bool:
        """Check if content is valid UTF-8."""
        try:
            content.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    
    def smart_decode(self, content: bytes) -> str:
        """
        Smart decode with multiple fallbacks.
        
        Returns decoded string, handling various edge cases.
        """
        # Try common encodings in order
        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        
        for encoding in encodings:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        
        # Last resort: decode with ignore errors
        return content.decode("utf-8", errors="ignore")