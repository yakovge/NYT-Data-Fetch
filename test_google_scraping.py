#!/usr/bin/env python3
"""Test Google AI with actual NYT scraping scenario."""

import asyncio
from nyt_scraper.parsing.micro_ai_controller import MicroAIController
from nyt_scraper.parsing.heavy_ai_fallback import HeavyAIFallback
from nyt_scraper.models import Article
from nyt_scraper.ai import AIClient
from datetime import datetime

# Sample NYT-style HTML content
sample_nyt_html = """
<html>
<head>
    <title>Breaking: Major News Development</title>
    <meta property="og:title" content="Breaking: Major News Development">
    <meta property="article:author" content="Jane Reporter">
    <meta property="article:section" content="World">
</head>
<body>
    <article>
        <h1>Breaking: Major News Development</h1>
        <div class="byline">By Jane Reporter</div>
        <div class="dateline">Dec 15, 2024</div>
        <div class="story-content">
            <p>This is a sample news article content for testing the Google AI integration.</p>
            <p>The article discusses important developments in world affairs that require careful analysis.</p>
            <p>Multiple paragraphs demonstrate the AI's ability to extract complete article content.</p>
        </div>
    </article>
</body>
</html>
"""

async def test_google_micro_ai():
    """Test MicroAI Controller with Google AI."""
    print("Testing MicroAI Controller with Google AI")
    print("=" * 50)
    
    # Create AI client configured for Google
    ai_client = AIClient()
    print(f"Using AI Provider: {ai_client.current_provider_name}")
    
    # Create MicroAI controller
    micro_ai = MicroAIController(ai_client=ai_client)
    
    # Create a test article with missing metadata
    test_article = Article(
        source_url="https://www.nytimes.com/2024/12/15/world/test-article.html",
        canonical_url="https://www.nytimes.com/2024/12/15/world/test-article.html",
        title="Breaking: Major News Development",
        body="Sample content...",
        discovered_at=datetime.utcnow()
    )
    # Remove author to test gap-filling
    test_article.author = None
    test_article.section = None
    
    print(f"Original Article:")
    print(f"  Title: {test_article.title}")
    print(f"  Author: {test_article.author}")
    print(f"  Section: {test_article.section}")
    print()
    
    # Test gap filling
    print("Testing metadata gap-filling with Google AI...")
    success, metadata = await micro_ai.fill_missing_metadata(test_article, sample_nyt_html)
    
    if success:
        print("SUCCESS! Google AI filled missing metadata:")
        for key, value in metadata.items():
            print(f"  {key}: {value}")
    else:
        print("No metadata extracted")
    
    print()

async def test_google_heavy_ai():
    """Test Heavy AI Fallback with Google AI."""
    print("Testing Heavy AI Fallback with Google AI")
    print("=" * 50)
    
    # Create AI client configured for Google
    ai_client = AIClient()
    
    # Create Heavy AI fallback
    heavy_ai = HeavyAIFallback(ai_client=ai_client)
    
    print(f"Using AI Provider: {heavy_ai.ai_client.current_provider_name}")
    
    # Test article quality assessment
    test_article = Article(
        source_url="https://www.nytimes.com/2024/12/15/world/test-article.html",
        title="Short title",  # This might trigger quality concerns
        body="Very short content.",  # This will definitely trigger escalation
        discovered_at=datetime.utcnow()
    )
    
    print("Testing content quality assessment...")
    should_escalate, reason = heavy_ai.should_escalate(
        test_article, 
        parse_confidence=0.6,  # Low confidence to trigger escalation
        total_requests_today=100
    )
    
    print(f"Should escalate to heavy AI: {should_escalate}")
    print(f"Reason: {reason}")
    
    if should_escalate:
        print("\nTesting full article extraction with Google AI...")
        try:
            success, extracted_data = await heavy_ai.extract_full_article(sample_nyt_html, test_article.source_url)
            
            if success:
                print("SUCCESS! Google AI extracted full article:")
                print(f"  Title: {extracted_data.get('title', 'N/A')}")
                print(f"  Author: {extracted_data.get('author', 'N/A')}")
                print(f"  Body length: {len(extracted_data.get('body', ''))} chars")
                print(f"  Section: {extracted_data.get('section', 'N/A')}")
            else:
                print(f"Extraction failed: {extracted_data}")
                
        except Exception as e:
            print(f"Error during extraction: {e}")

async def main():
    """Run all Google AI tests."""
    print("Google AI Integration Test for NYT Scraper")
    print("=" * 60)
    print()
    
    await test_google_micro_ai()
    print()
    await test_google_heavy_ai()
    
    print()
    print("=" * 60)
    print("Google AI Integration Test Complete!")
    print("Your NYT scraper is now using Google Gemini models!")

if __name__ == "__main__":
    asyncio.run(main())