"""
Example: How to use AstrbotKVCache in Overstats modules.

This file demonstrates how to use the global cache instance
for caching API responses and other data.

Storage strategy:
- Small data (JSON, metadata) → KV storage (persistent across reloads)
- Large files (images, binary) → File storage in plugin_data directory
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .kv_adapter import get_global_cache


async def example_cache_api_response(
    bnet_id: str,
    fetch_func,
    max_age_seconds: int = 300,
) -> Dict[str, Any]:
    """
    Example: Cache an API response for 5 minutes.

    Usage:
        result = await example_cache_api_response(
            bnet_id="Player#12345",
            fetch_func=lambda: api_client.search_bnet_account("Player#12345"),
            max_age_seconds=300,
        )
    """
    cache = get_global_cache()
    if cache is None:
        # No cache available, fetch directly
        return await fetch_func()

    namespace = "api_response"
    key = f"bnet:{bnet_id}"

    return await cache.get_or_set_json(
        namespace=namespace,
        key=key,
        factory=fetch_func,
        max_age_seconds=max_age_seconds,
    )


async def example_cache_image(
    url: str,
    fetch_func,
) -> Optional[bytes]:
    """
    Example: Cache an image (binary data).

    Usage:
        image_bytes = await example_cache_image(
            url="https://example.com/hero.png",
            fetch_func=lambda: api_client.get_icon(url),
        )
    """
    cache = get_global_cache()
    if cache is None:
        return await fetch_func()

    namespace = "images"
    key = f"url:{url}"

    # Try to get from cache
    cached = await cache.get_bytes(namespace, key)
    if cached is not None:
        return cached

    # Fetch and cache
    data = await fetch_func()
    if data:
        await cache.set_bytes(namespace, key, data)
    return data


async def example_cache_with_prefix(
    prefix: str,
    key: str,
    fetch_func,
    max_age_seconds: int = 3600,
) -> Dict[str, Any]:
    """
    Example: Cache with a custom prefix for different data types.

    Usage:
        result = await example_cache_with_prefix(
            prefix="match_detail",
            key="match_id_12345",
            fetch_func=lambda: api_client.query_match_detail(token, match_id),
            max_age_seconds=600,
        )
    """
    cache = get_global_cache()
    if cache is None:
        return await fetch_func()

    return await cache.get_or_set_json(
        namespace=prefix,
        key=key,
        factory=fetch_func,
        max_age_seconds=max_age_seconds,
    )


# Example integration with existing code
async def example_resolve_customer_token(
    bnet_id: str,
    search_func,
) -> str:
    """
    Example: Resolve customer_token with caching.

    This shows how to integrate caching into the existing
    bnet_search flow.
    """
    cache = get_global_cache()
    if cache is None:
        result = await search_func(bnet_id)
        return result.get("data", {}).get("customerToken", "")

    namespace = "bnet_search"
    key = f"token:{bnet_id}"

    # Try cache first
    cached = await cache.get_json(namespace, key)
    if cached is not None:
        data = cached.get("data", cached)
        if isinstance(data, dict):
            return data.get("customerToken", "")

    # Fetch fresh data
    result = await search_func(bnet_id)
    if result.get("ok"):
        await cache.set_json(namespace, key, result)

    return result.get("data", {}).get("customerToken", "")
