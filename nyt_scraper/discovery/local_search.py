"""Local search using Whoosh index for offline queries."""

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from whoosh import fields, index
from whoosh.analysis import StandardAnalyzer
from whoosh.qparser import MultifieldParser, QueryParser
from whoosh.query import And, DateRange, Term

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article, ArticleStatus

logger = get_logger(__name__)


class LocalSearch:
    """Whoosh-based local search for cached articles."""
    
    def __init__(self):
        self.index_dir = Path("./data/search_index")
        self.index_dir.mkdir(parents=True, exist_ok=True)
        
        # Define Whoosh schema
        self.schema = fields.Schema(
            url=fields.ID(stored=True, unique=True),
            title=fields.TEXT(stored=True, analyzer=StandardAnalyzer()),
            body=fields.TEXT(analyzer=StandardAnalyzer()),
            author=fields.TEXT(stored=True),
            section=fields.KEYWORD(stored=True, lowercase=True),
            tags=fields.KEYWORD(stored=True, lowercase=True, commas=True),
            published_date=fields.DATETIME(stored=True),
            discovered_date=fields.DATETIME(stored=True),
            content_hash=fields.ID(stored=True)
        )
        
        self._index = None
        self._initialize_index()
    
    def _initialize_index(self):
        """Initialize or open the Whoosh index."""
        try:
            if index.exists_in(str(self.index_dir)):
                self._index = index.open_dir(str(self.index_dir))
                logger.info("search_index_opened", path=str(self.index_dir))
            else:
                self._index = index.create_in(str(self.index_dir), self.schema)
                logger.info("search_index_created", path=str(self.index_dir))
        except Exception as e:
            logger.error("search_index_error", error=str(e))
            raise
    
    def add_articles(self, articles: List[Article]):
        """Add or update articles in the search index."""
        if not articles:
            return
        
        writer = self._index.writer()
        
        try:
            for article in articles:
                # Convert article to index document
                doc = self._article_to_doc(article)
                writer.update_document(**doc)
            
            writer.commit()
            
            logger.info(
                "articles_indexed",
                count=len(articles),
                index_path=str(self.index_dir)
            )
            
        except Exception as e:
            writer.cancel()
            logger.error("indexing_error", error=str(e))
            raise
    
    def _article_to_doc(self, article: Article) -> Dict:
        """Convert Article to Whoosh document."""
        return {
            "url": article.source_url,
            "title": article.title or "",
            "body": article.body or "",
            "author": article.author or "",
            "section": article.section or "",
            "tags": ",".join(article.tags) if article.tags else "",
            "published_date": article.published_date or article.discovered_at,
            "discovered_date": article.discovered_at,
            "content_hash": article.body_hash or ""
        }
    
    def search(
        self,
        query: str,
        limit: int = 50,
        section: Optional[str] = None,
        author: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> List[Dict]:
        """
        Search articles in the local index.
        
        Args:
            query: Search query string
            limit: Maximum results to return
            section: Filter by section
            author: Filter by author
            date_from: Filter articles after this date
            date_to: Filter articles before this date
            
        Returns:
            List of matching articles as dictionaries
        """
        if not query.strip():
            return []
        
        with self._index.searcher() as searcher:
            # Build query
            parser = MultifieldParser(
                ["title", "body", "author", "tags"],
                schema=self._index.schema
            )
            
            # Parse main query
            parsed_query = parser.parse(query)
            
            # Add filters
            filters = []
            
            if section:
                filters.append(Term("section", section.lower()))
            
            if author:
                author_parser = QueryParser("author", schema=self._index.schema)
                filters.append(author_parser.parse(author))
            
            if date_from or date_to:
                date_range = DateRange(
                    "published_date",
                    date_from,
                    date_to
                )
                filters.append(date_range)
            
            # Combine query with filters
            if filters:
                final_query = And([parsed_query] + filters)
            else:
                final_query = parsed_query
            
            # Execute search
            results = searcher.search(final_query, limit=limit)
            
            # Convert results to dictionaries
            articles = []
            for hit in results:
                article_dict = dict(hit)
                article_dict["score"] = hit.score
                articles.append(article_dict)
            
            logger.info(
                "local_search_complete",
                query=query,
                results_found=len(articles),
                total_docs=searcher.doc_count()
            )
            
            return articles
    
    def search_similar(self, article: Article, limit: int = 10) -> List[Dict]:
        """Find similar articles using More Like This."""
        if not article.body:
            return []
        
        with self._index.searcher() as searcher:
            # Find the article's document
            results = searcher.search(Term("url", article.source_url), limit=1)
            
            if not results:
                return []
            
            # Get similar documents
            similar = results[0].more_like_this("body", top=limit)
            
            articles = []
            for hit in similar:
                article_dict = dict(hit)
                article_dict["similarity_score"] = hit.score
                articles.append(article_dict)
            
            logger.debug(
                "similar_articles_found",
                source_url=article.source_url,
                similar_count=len(articles)
            )
            
            return articles
    
    def get_trending_topics(self, days: int = 7, limit: int = 20) -> List[Dict]:
        """Get trending topics from recent articles."""
        from datetime import timedelta
        
        since = datetime.utcnow() - timedelta(days=days)
        
        with self._index.searcher() as searcher:
            # Search recent articles
            date_query = DateRange("published_date", since, None)
            results = searcher.search(date_query, limit=1000)  # Get many recent articles
            
            # Count tag frequencies
            tag_counts = {}
            for hit in results:
                tags_str = hit.get("tags", "")
                if tags_str:
                    tags = [tag.strip() for tag in tags_str.split(",")]
                    for tag in tags:
                        if tag:
                            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            
            # Sort by frequency
            trending = sorted(
                tag_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:limit]
            
            return [
                {"tag": tag, "count": count, "trend_score": count / days}
                for tag, count in trending
            ]
    
    def get_section_stats(self) -> Dict[str, int]:
        """Get article count by section."""
        with self._index.searcher() as searcher:
            # Get all unique sections
            sections = {}
            for doc in searcher.all_doc_ids():
                stored_fields = searcher.stored_fields(doc)
                section = stored_fields.get("section", "unknown")
                sections[section] = sections.get(section, 0) + 1
            
            return sections
    
    def optimize_index(self):
        """Optimize the search index for better performance."""
        try:
            writer = self._index.writer()
            writer.commit(optimize=True)
            
            logger.info("search_index_optimized")
            
        except Exception as e:
            logger.error("index_optimization_error", error=str(e))
    
    def cleanup_old_articles(self, days: int = 30):
        """Remove articles older than specified days from index."""
        from datetime import timedelta
        
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        with self._index.searcher() as searcher:
            writer = self._index.writer()
            
            try:
                # Find old articles
                old_query = DateRange("published_date", None, cutoff)
                old_results = searcher.search(old_query, limit=None)
                
                # Delete them
                for hit in old_results:
                    writer.delete_by_term("url", hit["url"])
                
                writer.commit()
                
                logger.info(
                    "old_articles_cleaned",
                    deleted_count=len(old_results),
                    cutoff_date=cutoff.isoformat()
                )
                
            except Exception as e:
                writer.cancel()
                logger.error("cleanup_error", error=str(e))
                raise
    
    def get_index_stats(self) -> Dict:
        """Get search index statistics."""
        with self._index.searcher() as searcher:
            doc_count = searcher.doc_count()
            
            # Get index size
            index_size = sum(
                f.stat().st_size 
                for f in self.index_dir.glob("*") 
                if f.is_file()
            )
            
            # Get field statistics
            field_stats = {}
            for field_name in self.schema.names():
                try:
                    field_stats[field_name] = len(list(searcher.lexicon(field_name)))
                except Exception:
                    field_stats[field_name] = 0
            
            return {
                "total_documents": doc_count,
                "index_size_mb": index_size / (1024 * 1024),
                "index_path": str(self.index_dir),
                "schema_fields": list(self.schema.names()),
                "field_statistics": field_stats
            }
    
    def suggest_corrections(self, query: str) -> List[str]:
        """Suggest spelling corrections for query."""
        with self._index.searcher() as searcher:
            corrector = searcher.corrector("title")
            suggestions = corrector.suggest(query, limit=5)
            
            return list(suggestions)
    
    def close(self):
        """Close the search index."""
        if self._index:
            self._index.close()
            logger.info("search_index_closed")