#!/usr/bin/env python3
"""从现有 ChatStore 历史对话迁移数据到 OpenViking 记忆层。

用法:
    python -m scripts.migrate_to_viking
    # 或
    python scripts/migrate_to_viking.py

前置条件:
    1. OpenViking 服务已启动 (默认 http://localhost:1933)
    2. 已设置 OPENVIKING_URL / OPENVIKING_API_KEY 环境变量（如需）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp

from valveye.config import settings


async def migrate() -> None:
    url = settings.openviking_url.rstrip("/")
    api_key = settings.openviking_api_key

    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    async with aiohttp.ClientSession(headers=headers) as session:
        # 健康检查
        try:
            async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    print(f"错误: OpenViking 服务不可用 (status={resp.status})")
                    sys.exit(1)
        except Exception as exc:
            print(f"错误: 无法连接 OpenViking 服务 ({exc})")
            sys.exit(1)

        print("✓ OpenViking 服务已连接")

        # 创建记忆目录
        for dir_uri in [
            "viking://user/memories/",
            "viking://user/preferences/",
            "viking://user/entities/",
        ]:
            try:
                await session.post(f"{url}/api/v1/fs/mkdir", json={"uri": dir_uri})
            except Exception:
                pass

        # 扫描 ChatStore 历史文件
        chat_dir = Path.home() / ".valveye" / "chats"
        if not chat_dir.exists():
            print(f"未找到历史对话目录: {chat_dir}")
            return

        json_files = sorted(chat_dir.glob("*.json"))
        if not json_files:
            print("未找到历史对话文件")
            return

        print(f"找到 {len(json_files)} 个历史对话，开始迁移...")

        migrated = 0
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  跳过 {jf.name}: {exc}")
                continue

            thread_id = data.get("thread_id", jf.stem)
            messages = data.get("messages", [])
            if not messages:
                continue

            # 创建 session
            await session.post(
                f"{url}/api/v1/sessions",
                json={"session_id": thread_id},
                timeout=aiohttp.ClientTimeout(total=5),
            )

            # 写入消息
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if not content or role == "tool":
                    continue
                ov_role = "assistant" if role == "assistant" else "user"
                await session.post(
                    f"{url}/api/v1/sessions/{thread_id}/messages",
                    json={"role": ov_role, "content": content},
                    timeout=aiohttp.ClientTimeout(total=5),
                )

            # 提交 session 触发记忆提取
            async with session.post(
                f"{url}/api/v1/sessions/{thread_id}/commit",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    migrated += 1
                    title = data.get("title", thread_id[:8])
                    print(f"  ✓ {title} ({len(messages)} 条消息)")
                else:
                    print(f"  ⚠ {jf.name}: commit 返回 {resp.status}")

        print(f"\n迁移完成: {migrated}/{len(json_files)} 个对话已提交")
        print("长期记忆提取将在后台异步完成，请稍后通过 viking://user/memories/ 查看。")


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
