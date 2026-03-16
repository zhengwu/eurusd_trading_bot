"""Seen-article cache to prevent re-processing.

Keyed on SHA-256(url). Backed by a flat text file (one hash per line).
In-memory set for O(1) lookup; written to disk on each new entry.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)


class DedupCache:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._seen: set[str] | None = None

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha256(url.strip().encode()).hexdigest()

    def _load(self) -> set[str]:
        if self._seen is not None:
            return self._seen
        seen: set[str] = set()
        if self.cache_path.exists():
            try:
                for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                    h = line.strip()
                    if h:
                        seen.add(h)
            except Exception as e:
                logger.warning(f"DedupCache load failed: {e}")
        self._seen = seen
        return seen

    # ── public API ────────────────────────────────────────────────────────────

    def is_seen(self, url: str) -> bool:
        return self._hash(url) in self._load()

    def mark_seen(self, url: str) -> None:
        h = self._hash(url)
        seen = self._load()
        if h in seen:
            return
        seen.add(h)
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("a", encoding="utf-8") as f:
                f.write(h + "\n")
        except Exception as e:
            logger.warning(f"DedupCache write failed: {e}")

    def filter_new(self, items: list[dict], url_key: str = "url") -> list[dict]:
        """Return only items whose URL has not been seen. Marks new ones as seen."""
        new = []
        for item in items:
            url = item.get(url_key) or ""
            if not url:
                continue
            if not self.is_seen(url):
                self.mark_seen(url)
                new.append(item)
        return new

    def clear(self) -> None:
        """Reset the cache (call at end of day)."""
        self._seen = set()
        try:
            self.cache_path.write_text("", encoding="utf-8")
        except Exception as e:
            logger.warning(f"DedupCache clear failed: {e}")
