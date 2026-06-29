"""Infra regression tests for audit P2 fixes: bounded TTLCache + cryptonews
XML-expansion guard."""
from __future__ import annotations

import pytest


def test_ttlcache_is_bounded():
    """The cache must evict oldest entries past its cap (memory-leak guard)."""
    from tckr.cache import TTLCache
    c = TTLCache(max_entries=10)
    for i in range(50):
        c.put((i,), i)
    assert len(c._d) <= 10
    # The most-recently written keys survive; the oldest are evicted.
    assert c.get((49,), ttl_s=1e9) == 49
    assert c.get((0,), ttl_s=1e9) is None


def test_ttlcache_put_refreshes_recency():
    from tckr.cache import TTLCache
    c = TTLCache(max_entries=3)
    c.put(("a",), 1)
    c.put(("b",), 2)
    c.put(("c",), 3)
    c.put(("a",), 11)        # refresh 'a' -> 'b' is now oldest
    c.put(("d",), 4)         # evicts the oldest, which should be 'b'
    assert c.get(("a",), 1e9) == 11
    assert c.get(("b",), 1e9) is None
    assert c.get(("d",), 1e9) == 4


@pytest.mark.asyncio
async def test_cryptonews_rejects_doctype_feed(monkeypatch):
    """A feed declaring a DTD/ENTITY (billion-laughs vector) must be refused
    before parsing, returning None rather than expanding."""
    from tckr import _http, cryptonews

    bomb = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE rss [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<rss><channel><item><title>&b;</title>'
        '<link>http://x/1</link></item></channel></rss>'
    )

    async def fake_get_text(*args, **kwargs):
        return bomb

    cryptonews._cache._d.clear()  # avoid a hit from the live smoke test
    monkeypatch.setattr(_http, "get_text", fake_get_text)
    out = await cryptonews.feed("decrypt")
    assert out is None


@pytest.mark.asyncio
async def test_cryptonews_parses_clean_feed(monkeypatch):
    from tckr import _http, cryptonews

    feed = (
        '<?xml version="1.0"?><rss><channel>'
        '<item><title>Hello</title><link>http://x/1</link>'
        '<pubDate>Sun, 28 Jun 2026 22:00:00 +0000</pubDate>'
        '<description>body</description></item>'
        '</channel></rss>'
    )

    async def fake_get_text(*args, **kwargs):
        return feed

    cryptonews._cache._d.clear()  # avoid a hit from the live smoke test
    monkeypatch.setattr(_http, "get_text", fake_get_text)
    out = await cryptonews.feed("decrypt")
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["title"] == "Hello"
    assert out[0]["url"] == "http://x/1"
    assert out[0]["published_ts"] is not None
