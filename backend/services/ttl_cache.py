"""进程内 TTL 缓存（2026-10-12）。

为什么需要（用户反馈"分时加载不够流畅"，实测后端数字）：
  `/api/intraday/quotes?codes=<65>` 105ms、`mode=live` 85ms、`mode=store` 4ms、**5 分钟档 466ms**。
  ⇒ 瓶颈在**实时源的 HTTP**与**每次重算的 5 分钟聚合**，不在本地存储 —— 所以**不引内存数据库**（省不到 4ms），
    只在进程内加一层极短的 TTL：轮询类 2 秒、明细类 10 秒，多标签页/多组件共享，重复请求不再打实时源。

设计：`dict` + `threading.Lock` + `time.monotonic()`；键 = 调用参数；带 `cache_clear()`；TTL 到期即失效（不主动清理，量级只有几十个键）。
"""
from __future__ import annotations

import functools
import threading
import time
from typing import Callable

_STATS = {"hits": 0, "misses": 0}


def ttl_cache(seconds: float, *, name: str = ""):
    """把函数结果缓存 `seconds` 秒（键 = 全部位置/关键字参数）。"""

    def decorator(fn: Callable):
        store: dict = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                key = (args, tuple(sorted(kwargs.items())))
                hash(key)
            except TypeError:                      # 参数不可哈希（极少见）→ 直通，不缓存
                return fn(*args, **kwargs)
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit is not None and now - hit[0] < seconds:
                    _STATS["hits"] += 1
                    return hit[1]
                _STATS["misses"] += 1
            value = fn(*args, **kwargs)
            with lock:
                store[key] = (now, value)
            return value

        def cache_clear() -> None:
            with lock:
                store.clear()

        wrapper.cache_clear = cache_clear          # type: ignore[attr-defined]
        wrapper.cache_name = name or fn.__name__   # type: ignore[attr-defined]
        wrapper.cache_seconds = seconds            # type: ignore[attr-defined]
        wrapper.cache_size = lambda: len(store)    # type: ignore[attr-defined]
        return wrapper

    return decorator


def cache_stats() -> dict:
    """给自检/测试用：命中与未命中次数。"""
    return dict(_STATS)
