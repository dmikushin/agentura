"""Ogham-based team board — replaces JSONL with semantic memory.

Thin adapter between agentura's board HTTP API and ogham's database/service layer.
Each team gets an ogham profile "team:{name}" for isolation.
"""

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def _ensure_init():
    """Lazy-initialize ogham backend on first use."""
    global _initialized
    if _initialized:
        return

    # ogham reads config from env vars via pydantic-settings
    # Ensure DATABASE_BACKEND=postgres is set (docker-compose provides it)
    os.environ.setdefault("DATABASE_BACKEND", "postgres")

    from ogham.database import get_backend
    backend = get_backend()

    # Initialize connection pool if postgres backend
    if hasattr(backend, "initialize"):
        backend.initialize()

    _initialized = True
    logger.info("Ogham board backend initialized")


def _profile(team_name: str) -> str:
    return f"team:{team_name}"


def post(team_name: str, author: str, text: str, tags: list[str] | None = None) -> dict:
    """Store a board entry as an ogham memory."""
    _ensure_init()

    from ogham.service import store_memory_enriched

    all_tags = [f"team:{team_name}"]
    if tags:
        all_tags.extend(tags)

    try:
        result = store_memory_enriched(
            content=text,
            profile=_profile(team_name),
            source=author,
            tags=all_tags,
            metadata={"author": author, "board_entry": True},
            auto_link=True,
        )
        return {"status": "ok", "id": str(result.get("id", ""))}
    except Exception as e:
        logger.error(f"ogham store failed: {e}")
        return {"status": "error", "error": str(e)}


def recent(team_name: str, limit: int = 50, offset: int = 0) -> dict:
    """List recent board entries."""
    _ensure_init()

    from ogham.database import list_recent_memories

    try:
        # ogham doesn't have offset, fetch limit+offset and slice
        entries = list_recent_memories(
            profile=_profile(team_name),
            limit=limit + offset,
        )
        entries = entries[offset:]

        return {
            "status": "ok",
            "entries": [_format_entry(e) for e in entries],
            "total": len(entries),
        }
    except Exception as e:
        logger.error(f"ogham list failed: {e}")
        return {"status": "ok", "entries": [], "total": 0}


def search(team_name: str, query: str, limit: int = 20) -> dict:
    """Hybrid semantic + full-text search on board entries."""
    _ensure_init()

    from ogham.service import search_memories_enriched

    try:
        results = search_memories_enriched(
            query=query,
            profile=_profile(team_name),
            limit=limit,
        )
        return {
            "status": "ok",
            "entries": [_format_entry(e) for e in results],
            "total": len(results),
        }
    except Exception as e:
        logger.error(f"ogham search failed: {e}")
        return {"status": "ok", "entries": [], "total": 0}


def _format_entry(mem: dict) -> dict:
    """Convert ogham memory to board entry format."""
    metadata = mem.get("metadata") or {}
    return {
        "id": str(mem.get("id", "")),
        "author": metadata.get("author", mem.get("source", "")),
        "text": mem.get("content", ""),
        "timestamp": str(mem.get("created_at", "")),
        "importance": mem.get("importance"),
        "tags": mem.get("tags", []),
        "similarity": mem.get("similarity"),  # present in search results
    }
