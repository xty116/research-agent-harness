import httpx

from research_agent.providers import Sources


async def test_partial_rss_failure_preserves_usable_evidence_and_is_visible(settings, store):
    class HTTP:
        async def request(self, method, url):
            if "broken" in url:
                raise httpx.ConnectError("upstream unavailable")
            return b'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title><item><title>New result</title><link>https://example.com/result</link><description>Evidence</description></item></channel></rss>', url, {}

    settings.sources.rss_feeds = ["https://example.com/feed", "https://broken.example/feed"]
    result = await Sources(settings, store, HTTP()).rss()
    assert len(result) == 1
    assert result.failures == ["broken.example: ConnectError"]


async def test_paper_markdown_preserves_original_publication_date(settings, store):
    class HTTP:
        async def request(self, method, url):
            return (b"Title: A paper\nPublished Time: Fri, 30 Oct 2026 01:03:44 GMT\n\nFull paper original evidence with enough readable content.",
                    url, {"content-type": "text/markdown"})

    document = await Sources(settings, store, HTTP()).fetch("https://huggingface.co/papers/example.md")
    assert document.published_at == "2026-10-30"
    assert document.kind == "full_text"
