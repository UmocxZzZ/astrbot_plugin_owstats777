"""
Astrbot KV storage adapter for Overstats caching.

Provides a unified cache interface that uses Astrbot's KV storage
for persistent caching across plugin reloads.

Storage strategy:
- Small data (JSON, metadata) → KV storage
- Large files (images, binary) → File storage in plugin_data directory
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional


# Type aliases for Astrbot KV methods
KVGetFunc = Callable[[str, Any], Coroutine[Any, Any, Any]]
KVPutFunc = Callable[[str, Any], Coroutine[Any, Any, None]]
KVDeleteFunc = Callable[[str], Coroutine[Any, Any, None]]


def _hash_key(key: str) -> str:
    """Hash a key to ensure it's safe for KV storage."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _get_plugin_data_dir() -> Path:
    """Get the plugin data directory for large file storage."""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_owstats777" / "cache"
    except ImportError:
        # Fallback: use temp directory
        return Path(tempfile.gettempdir()) / "overstats_cache"


class AstrbotKVCache:
    """Astrbot KV storage adapter for Overstats caching."""

    def __init__(
        self,
        kv_get: KVGetFunc,
        kv_put: KVPutFunc,
        kv_delete: KVDeleteFunc,
        prefix: str = "ow_cache",
    ) -> None:
        self._get = kv_get
        self._put = kv_put
        self._delete = kv_delete
        self._prefix = prefix

    def _make_key(self, namespace: str, key: str) -> str:
        """Build a prefixed cache key."""
        hashed = _hash_key(key)
        return f"{self._prefix}:{namespace}:{hashed}"

    # ─── JSON cache ───────────────────────────────────────────

    async def get_json(self, namespace: str, key: str) -> Optional[dict]:
        """Get JSON data from cache. Returns None if not found."""
        full_key = self._make_key(namespace, key)
        raw = await self._get(full_key, None)
        if raw is None:
            return None
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            # Extract data from wrapper format
            if isinstance(payload, dict) and "data" in payload and "cached_at" in payload:
                return payload["data"]
            return payload
        except (json.JSONDecodeError, TypeError):
            return None

    async def set_json(self, namespace: str, key: str, value: dict) -> None:
        """Store JSON data in cache."""
        full_key = self._make_key(namespace, key)
        payload = {
            "data": value,
            "cached_at": int(time.time()),
        }
        await self._put(full_key, json.dumps(payload, ensure_ascii=False))

    async def get_json_with_meta(self, namespace: str, key: str) -> Optional[dict]:
        """Get JSON data with metadata (cached_at timestamp). Returns None if not found."""
        full_key = self._make_key(namespace, key)
        raw = await self._get(full_key, None)
        if raw is None:
            return None
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None

    # ─── Binary cache (file storage) ──────────────────────────

    def _get_cache_path(self, namespace: str, key: str) -> Path:
        """Get the file path for a cached binary item."""
        hashed = _hash_key(key)
        cache_dir = _get_plugin_data_dir() / namespace
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{hashed}.cache"

    async def get_bytes(self, namespace: str, key: str) -> Optional[bytes]:
        """Get binary data from cache. Returns None if not found."""
        cache_path = self._get_cache_path(namespace, key)
        if not cache_path.exists():
            return None
        try:
            return cache_path.read_bytes()
        except Exception:
            return None

    async def set_bytes(self, namespace: str, key: str, value: bytes) -> None:
        """Store binary data in cache (file storage)."""
        cache_path = self._get_cache_path(namespace, key)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp file first, then rename for atomicity
        fd, temp_path = tempfile.mkstemp(
            prefix=f"{cache_path.stem}.",
            suffix=".tmp",
            dir=str(cache_path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(value)
            Path(temp_path).replace(cache_path)
        except Exception:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def delete_bytes(self, namespace: str, key: str) -> None:
        """Delete a cached binary file."""
        cache_path = self._get_cache_path(namespace, key)
        if cache_path.exists():
            try:
                cache_path.unlink()
            except OSError:
                pass

    # ─── Generic cache ────────────────────────────────────────

    async def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """Get any value from cache."""
        full_key = self._make_key(namespace, key)
        return await self._get(full_key, default)

    async def set(self, namespace: str, key: str, value: Any) -> None:
        """Store any value in cache."""
        full_key = self._make_key(namespace, key)
        await self._put(full_key, value)

    # ─── Cache management ─────────────────────────────────────

    async def delete(self, namespace: str, key: str) -> None:
        """Delete a cached entry."""
        full_key = self._make_key(namespace, key)
        await self._delete(full_key)

    async def get_or_set_json(
        self,
        namespace: str,
        key: str,
        factory: Callable[[], Coroutine[Any, Any, dict]],
        max_age_seconds: int = 3600,
    ) -> dict:
        """Get JSON from cache, or compute and store it if missing/expired."""
        cached = await self.get_json_with_meta(namespace, key)
        if cached is not None:
            cached_at = cached.get("cached_at", 0)
            if time.time() - cached_at < max_age_seconds:
                return cached.get("data", cached)

        # Compute fresh value
        value = await factory()
        await self.set_json(namespace, key, value)
        return value


def create_kv_cache(
    kv_get: KVGetFunc,
    kv_put: KVPutFunc,
    kv_delete: KVDeleteFunc,
    prefix: str = "ow_cache",
) -> AstrbotKVCache:
    """Create an AstrbotKVCache instance."""
    return AstrbotKVCache(kv_get, kv_put, kv_delete, prefix)


# ─── Global cache instance ─────────────────────────────────
# Initialized by the plugin at startup, used by Overstats modules.

_global_cache: Optional[AstrbotKVCache] = None


def get_global_cache() -> Optional[AstrbotKVCache]:
    """Get the global cache instance. Returns None if not initialized."""
    return _global_cache


def set_global_cache(cache: AstrbotKVCache) -> None:
    """Set the global cache instance."""
    global _global_cache
    _global_cache = cache


def init_global_cache(
    kv_get: KVGetFunc,
    kv_put: KVPutFunc,
    kv_delete: KVDeleteFunc,
    prefix: str = "ow_cache",
) -> AstrbotKVCache:
    """Initialize and return the global cache instance."""
    cache = create_kv_cache(kv_get, kv_put, kv_delete, prefix)
    set_global_cache(cache)
    return cache