"""NYT Scraper - Production-ready article extraction system."""

__version__ = "1.0.0"
__author__ = "NYT Scraper Team"

from .models import Article
from .scraper import NYTScraper

__all__ = ["Article", "NYTScraper"]