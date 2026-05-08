from __future__ import annotations

import logging
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


class VikingMemory:
    """OpenViking 记忆层封装，提供 auto-recall / auto-capture 能力。

    所有方法均为异步，通过 HTTP REST API 与 OpenViking 服务端通信。
    recall 失败时静默降级（返回空字符串），不影响 Agent 正常工作。
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

    # ── Directory initialization ─────────────────────────────────────────

    async def ensure_dirs(self) -> None:
        """首次启动时创建记忆目录结构。"""
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
        失败时返回空字符串（静默降级）。
        """
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

            # 按 score 降序，逐条读取直到 token 预算耗尽
            memories: list[str] = []
            budget = self._token_budget
            for r in resources:
                if budget <= 0:
                    break
                uri = r.get("uri", "")
                score = r.get("score", 0.0)
                # 低于阈值的跳过
                if score < 0.3:
                    continue
                # 优先读 L1 overview，fallback 到 L0 abstract
                content = await self._read_with_fallback(uri)
                if not content:
                    continue
                # 粗略估算 token（中文约 1.5 char/token）
                est_tokens = len(content) // 2
                if est_tokens > budget:
                    # 截断到预算内
                    max_chars = budget * 2
                    content = content[:max_chars] + "..."
                memories.append(f"[记忆:{uri}] (相关度:{score:.2f})\n{content}")
                budget -= est_tokens

            return "\n---\n".join(memories) if memories else ""

        except Exception as exc:
            logger.debug("recall failed (degrading gracefully): %s", exc)
            return ""

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
        """
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
            logger.debug("capture failed (non-critical): %s", exc)

    async def _commit_async(self, session_id: str) -> None:
        """提交 session 触发长期记忆提取（异步，不阻塞）。"""
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
