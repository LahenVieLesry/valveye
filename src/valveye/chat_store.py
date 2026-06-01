from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

_TOOL_DISPLAY: dict[str, str] = {
    "query_low_price":        "查询价格",
    "compare_prices":         "对比区域价格",
    "search_similar_candidates": "搜索相似游戏",
    "get_game_details":       "获取游戏详情",
    "get_game_reviews":       "获取玩家评价",
    "recommend_similar_games": "推荐相似游戏",
    "subscribe_game":         "订阅价格提醒",
    "list_subscriptions":     "查看订阅列表",
}


class ChatStore:
    """File-based conversation persistence for the CLI chat."""

    def __init__(self, storage_dir: Path | str | None = None):
        if storage_dir is None:
            storage_dir = Path.home() / ".valveye" / "chats"
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Thread lifecycle ─────────────────────────────────────────────────

    def create_thread(self, context_seed: str = "") -> str:
        thread_id = str(uuid.uuid4())
        data = {
            "thread_id": thread_id,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "title": "新对话",
            "messages": [],
            "context_seed": context_seed,
        }
        self._write(thread_id, data)
        return thread_id

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        tool_name: str = "",
        tool_input: str = "",
        thinking: str = "",
    ) -> None:
        data = self._read(thread_id)
        if data is None:
            return
        msg: dict = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        if tool_name:
            msg["tool_name"] = tool_name
        if tool_input:
            msg["tool_input"] = tool_input
        if thinking:
            msg["thinking"] = thinking
        data["messages"].append(msg)
        # Auto-title from first user message
        if role == "user" and data.get("title") == "新对话":
            data["title"] = content[:60].replace("\n", " ")
        self._write(thread_id, data)

    def get_thread(self, thread_id: str) -> dict | None:
        return self._read(thread_id)

    def list_threads(self) -> list[dict]:
        threads: list[dict] = []
        for p in self._dir.glob("*.json"):
            data = self._read_file(p)
            if data is None:
                continue
            threads.append({
                "thread_id": data["thread_id"],
                "title": data.get("title", "新对话"),
                "created_at": data.get("created_at", ""),
                "message_count": len(data.get("messages", [])),
            })
        threads.sort(key=lambda t: t["created_at"], reverse=True)
        return threads

    # ── Export ───────────────────────────────────────────────────────────

    def export_markdown(self, thread_id: str) -> str:
        data = self._read(thread_id)
        if data is None:
            return ""
        lines: list[str] = []
        title = data.get("title", "对话记录")
        thread_id_short = data.get("thread_id", "")[:8]
        created = data.get("created_at", "")[:16].replace("T", " ")
        exported = f"{datetime.now():%Y/%m/%d %H:%M:%S}"

        lines.append("# 🤖 Valveye 对话记录")
        lines.append("")
        lines.append("> [!NOTE]")
        lines.append(f"> - **Session:** `{thread_id_short}`")
        lines.append(f"> - **标题:** {title}")
        lines.append(f"> - **开始时间:** {created}")
        lines.append(f"> - **导出时间:** {exported}")
        lines.append("")

        for msg in data["messages"]:
            role = msg["role"]
            content = msg["content"]
            lines.append("---")
            lines.append("")
            if role == "user":
                lines.append("### 👤 用户")
                lines.append("")
                lines.append(content)
                lines.append("")
            elif role == "assistant":
                thinking = msg.get("thinking", "")
                lines.append("### 💬 Valveye")
                lines.append("")
                if thinking:
                    lines.append("<details>")
                    lines.append(f"<summary>💭 思考过程 ({len(thinking)} 字)</summary>")
                    lines.append("")
                    lines.append(thinking)
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")
                lines.append(content)
                lines.append("")
            elif role == "tool":
                tool_name = msg.get("tool_name", "tool")
                tool_input = msg.get("tool_input", "")
                display_name = _TOOL_DISPLAY.get(tool_name, tool_name)
                lines.append(f"### ⚙ {display_name}")
                lines.append("")
                if tool_input:
                    lines.append(f"**{display_name}** — `{tool_input}`")
                    lines.append("")
                lines.append("<details>")
                lines.append("<summary>返回结果</summary>")
                lines.append("")
                lines.append("```")
                lines.append(content)
                lines.append("```")
                lines.append("")
                lines.append("</details>")
                lines.append("")

        return "\n".join(lines)

    def export_json(self, thread_id: str) -> str:
        data = self._read(thread_id)
        if data is None:
            return "{}"
        return json.dumps(data, ensure_ascii=False, indent=2)

    def export_html(self, thread_id: str) -> str:
        data = self._read(thread_id)
        if data is None:
            return ""
        title = html_escape(data.get("title", "对话记录"))
        thread_id_short = html_escape(data.get("thread_id", "")[:8])
        created = html_escape(data.get("created_at", "")[:16].replace("T", " "))
        exported = html_escape(f"{datetime.now():%Y/%m/%d %H:%M:%S}")
        msgs_html: list[str] = []

        for msg in data["messages"]:
            role = msg["role"]
            content = html_escape(msg["content"])
            if role == "user":
                msgs_html.append(
                    f'<div class="entry"><div class="entry-head user-head">👤 用户</div>'
                    f'<div class="bubble user-bubble">{content}</div></div>'
                )
            elif role == "assistant":
                thinking = html_escape(msg.get("thinking", ""))
                thinking_block = ""
                if thinking:
                    thinking_block = (
                        f'<details class="thinking-block">'
                        f'<summary>💭 思考过程 ({len(msg.get("thinking", ""))} 字)</summary>'
                        f'<pre>{thinking}</pre></details>'
                    )
                msgs_html.append(
                    f'<div class="entry"><div class="entry-head assistant-head">💬 Valveye</div>'
                    f'{thinking_block}'
                    f'<div class="bubble assistant-bubble">{content}</div></div>'
                )
            elif role == "tool":
                tool_name_raw = msg.get("tool_name", "tool") or "tool"
                display_name = html_escape(_TOOL_DISPLAY.get(tool_name_raw, tool_name_raw))
                tool_input_raw = msg.get("tool_input", "") or ""
                tool_input = html_escape(tool_input_raw)
                input_desc = f'<div class="tool-args">{display_name} — <code>{tool_input}</code></div>' if tool_input else ""
                msgs_html.append(
                    f'<div class="entry"><div class="entry-head tool-head">⚙ {display_name}</div>'
                    f'{input_desc}'
                    f'<details class="tool-block"><summary>返回结果</summary>'
                    f'<pre>{content}</pre></details></div>'
                )

        body = "\n".join(msgs_html)
        return _HTML_TEMPLATE.format(
            title=title, body=body,
            thread_id=thread_id_short, created=created, exported=exported,
        )

    # ── Internal I/O (atomic writes) ─────────────────────────────────────

    def _path(self, thread_id: str) -> Path:
        return self._dir / f"{thread_id}.json"

    def _read(self, thread_id: str) -> dict | None:
        return self._read_file(self._path(thread_id))

    def _read_file(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, thread_id: str, data: dict) -> None:
        target = self._path(thread_id)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 720px; margin: 2rem auto; padding: 0 1rem;
         background: #1e1e1e; color: #d4d4d4; }}
  h1 {{ font-size: 1.4rem; color: #e0e0e0; border-bottom: 1px solid #333; padding-bottom: .5rem; }}
  .meta {{ font-size: .8rem; color: #808080; margin-bottom: 1.5rem; }}
  .meta code {{ color: #9cdcfe; }}
  .entry {{ margin: 1rem 0; }}
  .entry-head {{ font-size: .85rem; font-weight: 600; margin-bottom: .3rem; }}
  .user-head {{ color: #569cd6; }}
  .assistant-head {{ color: #4ec9b0; }}
  .tool-head {{ color: #9cdcfe; }}
  .bubble {{ padding: .75rem 1rem; border-radius: 8px; line-height: 1.6; }}
  .user-bubble {{ background: #264f78; color: #fff; }}
  .assistant-bubble {{ background: #2d2d2d; }}
  .tool-args {{ font-size: .8rem; color: #808080; margin-bottom: .3rem; }}
  .tool-args code {{ color: #9cdcfe; }}
  details {{ margin: .4rem 0; }}
  summary {{ cursor: pointer; font-size: .8rem; color: #808080; padding: .3rem 0; }}
  summary:hover {{ color: #d4d4d4; }}
  .thinking-block {{ background: #1a1a2e; border-radius: 6px; padding: .5rem .75rem; }}
  .thinking-block summary {{ color: #c586c0; }}
  .thinking-block pre {{ color: #c586c0; font-size: .85rem; margin: .5rem 0 0; }}
  .tool-block {{ background: #1a1a2e; border-radius: 6px; padding: .5rem .75rem; }}
  .tool-block pre {{ color: #9cdcfe; font-size: .85rem; margin: .5rem 0 0; }}
  pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; }}
  hr {{ border: none; border-top: 1px solid #333; margin: 1.5rem 0; }}
</style>
</head>
<body>
<h1>🤖 {title}</h1>
<div class="meta">
  Session: <code>{thread_id}</code> · 开始: {created} · 导出: {exported}
</div>
{body}
</body>
</html>
"""
