"""
Astrbot KV storage adapter for Overstats caching.
"""
from .kv_adapter import (
    AstrbotKVCache,
    create_kv_cache,
    get_global_cache,
    init_global_cache,
    set_global_cache,
)

__all__ = [
    "AstrbotKVCache",
    "create_kv_cache",
    "get_global_cache",
    "init_global_cache",
    "set_global_cache",
]