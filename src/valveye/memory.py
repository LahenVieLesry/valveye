from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from valveye.config import settings

logger = logging.getLogger(__name__)

# ── Memory directory structure ───────────────────────────────────────────
_MEMORY_DIRS = [
    "viking://user/memories/",
    "viking://user/preferences/",
    "viking://user/entities/",
    "viking://agent/patterns/",
]


class FileMemoryBackend:
    """Lightweight local file-based memory backend used as fallback when OpenViking is unavailable.

    Stores conversation turns in JSONL files and performs keyword-based recall.
    """

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            base_dir = Path.home() / ".valveye" / "memories"
        self._dir = Path(base_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"

    async def ensure_dirs(self) -> None:
        """Create memory directory structure."""
        for sub in ("user/memories", "user/preferences", "user/entities", "agent/patterns"):
            (self._dir / sub).mkdir(parents=True, exist_ok=True)

    async def create_session(self, session_id: str) -> bool:
        """No-op for file backend — session files are created on first capture."""
        return True

    async def capture(self, session_id: str, user_msg: str, ai_msg: str) -> None:
        """Append a conversation turn to the session's JSONL file."""
        path = self._path(session_id)
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "user": user_msg,
            "assistant": ai_msg,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def recall(self, query: str, session_id: str = "", token_budget: int = 2000) -> str:
        """Keyword-based recall from all session files.

        Simple TF-IDF-like scoring: count keyword overlaps between query and stored messages.
        """
        if not query.strip():
            return ""

        query_words = set(query.lower().split())
        all_records: list[tuple[float, dict]] = []

        # Read all JSONL files
        for path in self._dir.glob("*.jsonl"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        text = f"{record.get('user', '')} {record.get('assistant', '')}"
                        text_words = set(text.lower().split())
                        overlap = len(query_words & text_words)
                        if overlap > 0:
                            all_records.append((overlap, record))
            except OSError:
                continue

        if not all_records:
            return ""

        # Sort by overlap score descending
        all_records.sort(key=lambda x: x[0], reverse=True)

        selected: list[str] = []
        budget = token_budget
        for score, record in all_records[:5]:
            if budget <= 0:
                break
            user_text = record.get("user", "")
            ai_text = record.get("assistant", "")
            content = f"[用户] {user_text[:200]}\n[助手] {ai_text[:200]}"
            est_tokens = len(content) // 2
            selected.append(content)
            budget -= est_tokens

        return "\n---\n".join(selected) if selected else ""

    async def write_memory(self, uri: str, content: str) -> bool:
        """Write a memory snippet to a file under the memories directory."""
        # Map viking URI to local path
        local_path = self._dir / uri.replace("viking://", "").replace("/", os.sep)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            local_path.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False


class VikingMemory:
    """OpenViking 记忆层封装，提供 auto-recall / auto-capture 能力。

    所有方法均为异步，通过 HTTP REST API 与 OpenViking 服务端通信。
    recall 失败时自动降级到 FileMemoryBackend（本地文件记忆）。
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        token_budget: int = 2000,
        commit_interval: int = 5,
    ):
        self._url = (url or settings.openviking_url).rstrip("/")
        self._api_key = api_key or settings.openviking_api_key
        self._token_budget = token_budget
        self._commit_interval = commit_interval
        self._session: aiohttp.ClientSession | None = None
        # 记录每个 thread 的轮次计数，用于决定何时 commit
        self._turn_counts: dict[str, int] = {}
        self._backend: str = "openviking"
        self._file_backend = FileMemoryBackend()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health_check(self) -> bool:
        """检查 OpenViking 服务是否可用。"""
        try:
            session = await self._ensure_session()
            async with session.get(f"{self._url}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _select_backend(self) -> None:
        """Auto-detect backend availability and switch to file fallback if needed."""
        if self._backend == "openviking":
            healthy = await self.health_check()
            if not healthy:
                logger.warning("OpenViking unavailable, falling back to FileMemoryBackend")
                self._backend = "file"
                await self._file_backend.ensure_dirs()

    # ── Directory initialization ─────────────────────────────────────────

    async def ensure_dirs(self) -> None:
        """首次启动时创建记忆目录结构。"""
        if self._backend == "file":
            await self._file_backend.ensure_dirs()
            return
        session = await self._ensure_session()
        for dir_uri in _MEMORY_DIRS:
            try:
                await session.post(
                    f"{self._url}/api/v1/fs/mkdir",
                    json={"uri": dir_uri},
                )
            except Exception as exc:
                logger.debug("mkdir %s failed (may already exist): %s", dir_uri, exc)

    # ── Session management ───────────────────────────────────────────────

    async def create_session(self, session_id: str) -> bool:
        """创建 OpenViking session，映射到 Valveye thread_id。"""
        await self._select_backend()
        if self._backend == "file":
            return await self._file_backend.create_session(session_id)
        try:
            session = await self._ensure_session()
            async with session.post(
                f"{self._url}/api/v1/sessions",
                json={"session_id": session_id},
            ) as resp:
                if resp.status in (200, 201, 409):  # 409 = already exists
                    self._turn_counts[session_id] = 0
                    return True
                logger.warning("create_session %s returned %d", session_id, resp.status)
                return False
        except Exception as exc:
            logger.warning("create_session failed: %s", exc)
            return False

    # ── Auto-Recall ──────────────────────────────────────────────────────

    async def recall(self, query: str, session_id: str = "") -> str:
        """Auto-recall: 从记忆库中语义检索相关记忆。

        返回格式化的记忆上下文字符串，可直接注入到 Agent 消息前缀。
        OpenViking 不可用时自动降级到 FileMemoryBackend。
        """
        await self._select_backend()
        if self._backend == "file":
            return await self._file_backend.recall(query, session_id, self._token_budget)

        if not query.strip():
            return ""
        try:
            session = await self._ensure_session()
            payload: dict[str, Any] = {
                "query": query,
                "limit": 5,
            }
            if session_id:
                payload["session_id"] = session_id

            async with session.post(
                f"{self._url}/api/v1/search/search",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()

            resources = data.get("resources", [])
            if not resources:
                return ""

            # Phase 1: Collect scored memories
            scored_memories: list[tuple[float, str, str]] = []  # (score, content, uri)
            for r in resources:
                uri = r.get("uri", "")
                score = r.get("score", 0.0)
                if score < 0.3:
                    continue
                content = await self._read_with_fallback(uri)
                if not content:
                    continue
                scored_memories.append((score, content, uri))

            if not scored_memories:
                return ""

            # Sort by relevance score descending (highest first)
            scored_memories.sort(key=lambda x: x[0], reverse=True)

            # Phase 2: Select memories within token budget
            # High-relevance memories get full inclusion;
            # lower-relevance memories get proportional truncation
            selected: list[str] = []
            budget = self._token_budget
            total_score = sum(s for s, _, _ in scored_memories)

            for score, content, uri in scored_memories:
                if budget <= 0:
                    break

                # Calculate proportional budget allocation based on relevance
                proportion = score / total_score if total_score > 0 else 0.2
                allocated_budget = max(50, int(budget * proportion * 2))  # Allow some overshoot

                est_tokens = len(content) // 2
                if est_tokens > allocated_budget:
                    # Truncate proportionally to allocated budget
                    max_chars = allocated_budget * 2
                    content = content[:max_chars] + "..."

                selected.append(f"[记忆:{uri}] (相关度:{score:.2f})\n{content}")
                budget -= min(est_tokens, allocated_budget)

            return "\n---\n".join(selected) if selected else ""

        except Exception as exc:
            logger.debug("recall failed (degrading to file backend): %s", exc)
            self._backend = "file"
            return await self._file_backend.recall(query, session_id, self._token_budget)

    async def _read_with_fallback(self, uri: str) -> str:
        """优先读 L1 overview，失败则读 L0 abstract。"""
        session = await self._ensure_session()
        # 尝试 L1
        try:
            async with session.get(
                f"{self._url}/api/v1/content/overview",
                params={"uri": uri},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("content", "")
                    if content:
                        return content
        except Exception:
            pass
        # fallback 到 L0
        try:
            async with session.get(
                f"{self._url}/api/v1/content/abstract",
                params={"uri": uri},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("content", "")
        except Exception:
            pass
        return ""

    # ── Auto-Capture ─────────────────────────────────────────────────────

    async def capture(self, session_id: str, user_msg: str, ai_msg: str) -> None:
        """Auto-capture: 将本轮对话消息记录到 OpenViking session。

        每 commit_interval 轮自动触发一次 commit 提取长期记忆。
        OpenViking 不可用时降级到 FileMemoryBackend。
        """
        await self._select_backend()
        if self._backend == "file":
            await self._file_backend.capture(session_id, user_msg, ai_msg)
            return
        try:
            session = await self._ensure_session()
            # 记录用户消息
            await session.post(
                f"{self._url}/api/v1/sessions/{session_id}/messages",
                json={"role": "user", "content": user_msg},
                timeout=aiohttp.ClientTimeout(total=5),
            )
            # 记录助手消息
            await session.post(
                f"{self._url}/api/v1/sessions/{session_id}/messages",
                json={"role": "assistant", "content": ai_msg},
                timeout=aiohttp.ClientTimeout(total=5),
            )

            # 更新轮次计数，决定是否 commit
            count = self._turn_counts.get(session_id, 0) + 1
            self._turn_counts[session_id] = count
            if count >= self._commit_interval:
                await self._commit_async(session_id)
                self._turn_counts[session_id] = 0

        except Exception as exc:
            logger.debug("capture failed (switching to file backend): %s", exc)
            self._backend = "file"
            await self._file_backend.capture(session_id, user_msg, ai_msg)

    async def _commit_async(self, session_id: str) -> None:
        """提交 session 触发长期记忆提取（异步，不阻塞）。"""
        if self._backend == "file":
            return
        try:
            session = await self._ensure_session()
            async with session.post(
                f"{self._url}/api/v1/sessions/{session_id}/commit",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    logger.info("session %s committed, long-term memory extraction started", session_id[:8])
                else:
                    logger.debug("commit returned %d", resp.status)
        except Exception as exc:
            logger.debug("commit failed: %s", exc)

    # ── Memory seeding ───────────────────────────────────────────────────

    async def write_memory(self, uri: str, content: str) -> bool:
        """直接写入记忆内容到指定 URI。"""
        await self._select_backend()
        if self._backend == "file":
            return await self._file_backend.write_memory(uri, content)
        try:
            session = await self._ensure_session()
            async with session.post(
                f"{self._url}/api/v1/content/write",
                json={"uri": uri, "content": content},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
        except Exception as exc:
            logger.debug("write_memory %s failed: %s", uri, exc)
            return False
