from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from valveye.agent import build_agent_executor, run_single_turn, stream_turn
from valveye.agent_tools import build_tools
from valveye.chat_store import ChatStore
from valveye.config import settings
from valveye.data_sources.cheapshark import CheapSharkSource
from valveye.data_sources.itad import ITADSource
from valveye.data_sources.steamdb import SteamDBSource
from valveye.game_data import GameDataService
from valveye.notifications import Notifier
from valveye.pricing import PriceService
from valveye.recommendation import Recommender
from valveye.scheduler import PriceCheckScheduler
from valveye.subscriptions import SubscriptionRepository

# ── Rich console ────────────────────────────────────────────────────────────
console = Console(highlight=False)

# ── Color tokens (ANSI-16 only, readable on both light & dark backgrounds) ──
# Primary accent   = cyan    (headings, user prompt)
# AI accent        = green   (AI label, success)
# Secondary        = dim     (borders, muted text)
# Warning / hint   = yellow  (usage hints)
# Tool accent      = blue    (tool call info)
# Thinking         = magenta (thinking phase indicator)

_SLASH_COMMANDS: dict[str, str] = {
    "/help":      "显示帮助信息",
    "/quit":      "退出对话",
    "/exit":      "退出对话",
    "/clear":     "清屏",
    "/query":     "查询游戏价格 · /query <游戏名>",
    "/recommend": "推荐相似游戏 · /recommend <游戏名>",
    "/subscribe": "订阅价格提醒 · /subscribe <游戏名>",
    "/list":      "查看当前订阅列表",
    "/history":   "显示对话轮数",
    "/model":     "显示当前模型信息",
    "/export":    "导出对话记录 · /export [md|json|html]",
    "/resume":    "恢复历史对话",
    "/new":       "开始新对话",
}

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


# ═══════════════════════════════════════════════════════════════════════════
#  UI helpers
# ═══════════════════════════════════════════════════════════════════════════

class _SlashCommandCompleter(Completer):
    """Completer that shows a command palette with descriptions when typing /."""

    def get_completions(self, document, complete_event):  # noqa: ARG002
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        word = text.lstrip("/").lower()
        for cmd, desc in _SLASH_COMMANDS.items():
            name = cmd.lstrip("/")
            if not word or name.startswith(word):
                yield Completion(
                    text=cmd,
                    start_position=-len(text),
                    display=name,
                    display_meta=desc,
                    style="bold ansicyan",
                    selected_style="bold ansicyan",
                )


def _build_keybindings() -> KeyBindings:
    """Build custom key bindings: double-Esc switch, cursor-aware up/down, / trigger."""
    kb = KeyBindings()
    _last_esc_time = 0.0

    @kb.add("escape", eager=True)
    def _(event):
        nonlocal _last_esc_time
        now = time.monotonic()
        if now - _last_esc_time < 0.4:
            _last_esc_time = 0.0
            event.app.exit(result="__switch__")
        _last_esc_time = now

    @kb.add("up")
    def _(event):
        buf = event.current_buffer
        if buf.complete_state is not None:
            buf.complete_previous()
            return
        if buf.cursor_position == 0:
            buf.history_backward()
            buf.cursor_position = len(buf.text)

    @kb.add("down")
    def _(event):
        buf = event.current_buffer
        if buf.complete_state is not None:
            buf.complete_next()
            return
        if buf.cursor_position == len(buf.text):
            if buf.history_forward():
                buf.cursor_position = len(buf.text)
            else:
                buf.reset()

    @kb.add("/")
    def _(event):
        buffer = event.app.current_buffer
        buffer.insert_text("/")
        if buffer.text.startswith("/"):
            buffer.start_completion(select_first=False)

    return kb


def _read_key() -> str:
    """Read a single keypress, returning escape sequences for special keys."""
    import tty
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return f"\x1b[{ch3}"
            return "escape"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _switch_menu(chat_store: ChatStore) -> str | None:
    """Interactive conversation switcher with arrow key and digit navigation.

    Returns thread_id, "__new__", "__clear__", or None (cancelled).
    """
    threads = chat_store.list_threads()
    # Build option list: [clear, new, ...threads]
    options: list[tuple[str, str, str]] = [
        ("__clear__", "清空输入", ""),
        ("__new__", "新对话", ""),
    ]
    for th in threads:
        created = th["created_at"][:16].replace("T", " ") if th["created_at"] else ""
        options.append((th["thread_id"], th["title"][:40], created))

    selected = 0  # default = clear input

    def _render() -> Table:
        t = Table(
            show_header=True, header_style="bold", show_lines=False,
            pad_edge=False, padding=(0, 2),
        )
        t.add_column("#", style="cyan", min_width=3)
        t.add_column("选项", min_width=30)
        t.add_column("时间", style="dim")
        for i, (_, title, ts) in enumerate(options):
            num = str(i) if i < 10 else ""
            if i == selected:
                t.add_row(
                    Text(num, style="bold cyan"),
                    Text(f"▸ {title}", style="bold"),
                    Text(ts, style="bold"),
                )
            else:
                t.add_row(num, f"  {title}", ts)
        return t

    with Live(console=console, refresh_per_second=15, transient=False) as live:
        live.update(
            Panel(_render(), title="[bold]切换对话[/]  ↑↓导航  数字选择  Enter确认  Esc取消",
                  border_style="dim", padding=(0, 1))
        )

        while True:
            key = _read_key()

            if key == "\x1b[A":  # Up arrow
                selected = (selected - 1) % len(options)
            elif key == "\x1b[B":  # Down arrow
                selected = (selected + 1) % len(options)
            elif key == "\r" or key == "\n":
                return options[selected][0]
            elif key == "escape":
                return None
            elif key.isdigit():
                idx = int(key)
                if idx < len(options):
                    if selected == idx:
                        return options[idx][0]
                    selected = idx
            else:
                continue

            live.update(
                Panel(_render(), title="[bold]切换对话[/]  ↑↓导航  数字选择  Enter确认  Esc取消",
                      border_style="dim", padding=(0, 1))
            )


def _history_path() -> Path:
    """Per-folder history file path."""
    p = Path.cwd() / ".valveye"
    p.mkdir(parents=True, exist_ok=True)
    return p / "history"


def _build_toolbar(thread_id: str, turn_count: int, chat_store: ChatStore, state: list):
    """Build a bottom_toolbar callable for PromptSession.

    state[0] = timestamp when hint should be shown (0 = no hint).
    """
    def _toolbar():
        thread = chat_store.get_thread(thread_id)
        title = (thread.get("title", "新对话") if thread else "新对话")[:30]
        model = settings.openai_model
        parts = [
            ("class:toolbar", f" {title} "),
            ("class:toolbar", f" · {model} "),
            ("class:toolbar", f" · 第{turn_count}轮 "),
        ]
        if state[0] and time.monotonic() - state[0] < 2.5:
            parts.append(("class:toolbar-dim", " · EscEsc切换对话"))
        return parts
    return _toolbar


_PROMPT_STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
    "toolbar": "bold ansiblack bg:ansicyan",
    "toolbar-dim": "ansiblack bg:ansicyan",
    "completion-menu": "noinherit",
    "completion-menu.completion": "noinherit",
    "completion-menu.completion.current": "bold ansicyan noinherit",
    "completion-menu.meta.completion": "ansigray noinherit",
    "completion-menu.meta.completion.current": "bold noinherit",
    "scrollbar": "noinherit",
    "scrollbar.background": "noinherit",
    "scrollbar.button": "noinherit",
})


def _pick_conversation(chat_store: ChatStore) -> str | None:
    """Show a numbered list of past conversations. Returns thread_id or None."""
    threads = chat_store.list_threads()
    console.print()
    if not threads:
        console.print("  [dim]暂无历史对话[/]")
        console.print()
        return None

    t = Table(
        show_header=True, header_style="bold", show_lines=False,
        pad_edge=False, padding=(0, 2),
    )
    t.add_column("#", style="cyan", min_width=3)
    t.add_column("标题", style="dim", min_width=30)
    t.add_column("消息数", style="dim", justify="right")
    t.add_column("时间", style="dim")
    t.add_row("0", "新对话", "", "")
    for i, th in enumerate(threads, 1):
        created = th["created_at"][:16].replace("T", " ") if th["created_at"] else ""
        t.add_row(str(i), th["title"][:40], str(th["message_count"]), created)
    console.print(Panel(t, title="[bold]选择对话[/]", border_style="dim", padding=(0, 1)))

    try:
        choice = input("  输入编号 (0=新对话, Enter=取消): ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None

    if not choice:
        return None
    if choice == "0":
        return "__new__"
    try:
        idx = int(choice)
        if 1 <= idx <= len(threads):
            return threads[idx - 1]["thread_id"]
    except ValueError:
        pass
    console.print("  [yellow]无效选择[/]")
    return None


def _show_welcome() -> None:
    """Compact one-shot banner."""
    console.print()
    console.print(
        "  [bold cyan]Valveye[/] [dim]v1.0[/]"
        "  ·  Steam 游戏价格顾问"
    )
    console.print(
        "  [dim]─────────────────────────────────────────────[/]"
    )
    console.print(
        "  输入消息开始对话  ·  [cyan]/help[/] 查看命令  ·  [cyan]/quit[/] 退出"
    )
    console.print()


def _show_help() -> None:
    t = Table(
        show_header=True, header_style="bold", show_lines=False,
        pad_edge=False, padding=(0, 2),
    )
    t.add_column("命令", style="cyan", min_width=14, no_wrap=True)
    t.add_column("说明", style="dim")
    for cmd, desc in _SLASH_COMMANDS.items():
        t.add_row(cmd, desc)
    console.print()
    console.print(Panel(t, title="[bold]命令列表[/]", border_style="dim", padding=(0, 1)))
    console.print()


def _show_model_info() -> None:
    lines = [
        f"  [cyan]模型[/]    {settings.openai_model}",
    ]
    if settings.openai_base_url:
        lines.append(f"  [cyan]API[/]     [dim]{settings.openai_base_url}[/]")
    console.print()
    for line in lines:
        console.print(line)
    console.print()


def _print_tool_call(raw: str) -> None:
    """Format a tool-call marker line:  ⚙ 查询价格 → The Bazaar"""
    inner = raw.strip().removeprefix("[调用工具:").removesuffix("]").strip()
    name = inner.split("(")[0].strip() if "(" in inner else inner
    args_part = inner[len(name):].strip().strip("()")
    display_name = _TOOL_DISPLAY.get(name, name)
    if args_part:
        console.print(f"  [blue]⚙[/]  [dim]{display_name}[/] [dim cyan]→ {args_part}[/]")
    else:
        console.print(f"  [blue]⚙[/]  [dim]{display_name}[/]")


def _print_thinking_panel(thinking_text: str, fold_state: str) -> None:
    """Render the thinking panel in folded or unfolded state."""
    if not thinking_text:
        return
    n = len(thinking_text)
    if fold_state == "folded":
        first_line = thinking_text.split("\n", 1)[0]
        preview = first_line[:100] + ("…" if len(first_line) > 100 else "")
        console.print(
            Panel(
                f"[dim]{preview}[/]\n"
                f"[dim]按 T 展开[/]  ·  [dim]{n} 字[/]",
                title="[magenta]💭 思考过程[/]",
                border_style="dim",
                padding=(0, 1),
            )
        )
    else:
        console.print(
            Panel(
                Text(thinking_text, style="dim"),
                title="[magenta]💭 思考过程[/]",
                border_style="dim",
                padding=(0, 1),
            )
        )


def _render_turn(thinking_text: str, fold_state: str, tool_parts: list[str], response_text: str) -> Group:
    """Build a Rich renderable for the full agent turn."""
    items = []
    if thinking_text:
        items.append(_build_thinking_panel(thinking_text, fold_state))
    for raw in tool_parts:
        items.append(_build_tool_call(raw))
    if response_text:
        items.append(Text(""))
        items.append(Markdown(response_text))
    items.append(Text(""))
    items.append(Text("  (T) 展开/折叠 · (Enter) 继续", style="dim"))
    return Group(*items)


def _build_tool_call(raw: str) -> Text:
    inner = raw.strip().removeprefix("[调用工具:").removesuffix("]").strip()
    name = inner.split("(")[0].strip() if "(" in inner else inner
    args_part = inner[len(name):].strip().strip("()")
    display_name = _TOOL_DISPLAY.get(name, name)
    if args_part:
        return Text(f"  ⚙  {display_name} → {args_part}", style="dim")
    return Text(f"  ⚙  {display_name}", style="dim")


def _summarize_tool_result(raw: str) -> str:
    """Parse a tool result string into a brief one-line summary."""
    text = raw.removeprefix("[工具结果:").removesuffix("]").strip()
    if not text:
        return ""
    # Price query result: "Title | 当前价 X CNY | 史低 Y CNY | 来源 Z | 在史低: True"
    if "当前价" in text and "史低" in text:
        return text.split("|")[0].strip() + " — " + " | ".join(p.strip() for p in text.split("|")[1:])
    # Error message
    if text.startswith("未找到") or text.startswith("无法获取") or text.startswith("暂无"):
        return text
    # JSON results — try to extract a brief summary
    if text.startswith("{") or text.startswith("["):
        try:
            import json as _json
            data = _json.loads(text)
            if isinstance(data, list):
                return f"{len(data)} 条结果"
            if isinstance(data, dict):
                title = data.get("title", "")
                if title:
                    return title
                return f"{len(data)} 个字段"
        except (ValueError, TypeError):
            pass
    # Fallback: truncate
    if len(text) > 120:
        return text[:120] + "…"
    return text


def _build_thinking_panel(thinking_text: str, fold_state: str) -> Panel:
    n = len(thinking_text)
    if fold_state == "folded":
        first_line = thinking_text.split("\n", 1)[0]
        preview = first_line[:100] + ("…" if len(first_line) > 100 else "")
        content = f"[dim]{preview}[/]\n[dim]按 T 展开[/]  ·  [dim]{n} 字[/]"
    else:
        content = Text(thinking_text, style="dim")
    return Panel(content, title="[magenta]💭 思考过程[/]", border_style="dim", padding=(0, 1))


# ═══════════════════════════════════════════════════════════════════════════
#  Agent turn — streaming thinking → fold → response
# ═══════════════════════════════════════════════════════════════════════════

async def _run_agent_turn(
    agent, message: str, thread_id: str,
    turn_count: int, fold_state: str = "folded",
    chat_store: ChatStore | None = None,
) -> tuple[int, str]:
    """Execute one agent turn.  Returns (new_turn_count, new_fold_state)."""

    thinking_parts: list[str] = []
    response_parts: list[str] = []
    tool_parts: list[str] = []
    tool_results: list[str] = []
    thinking_done = False

    def _live_group() -> Group:
        items: list = []
        if not thinking_done:
            items.append(Text("  [magenta]💭[/]  思考中…", style="dim"))
            items.append(Text("  " + "".join(thinking_parts)[-300:], style="dim"))
        for t in tool_parts:
            items.append(_build_tool_call(t))
        for r in tool_results:
            summary = _summarize_tool_result(r)
            if summary:
                items.append(Text(f"  [dim]  {summary}[/]"))
        if response_parts:
            items.append(Markdown("".join(response_parts)))
        return Group(*items)

    # ── Phase 1: live-stream thinking + response ─────────────────────────
    with Live(
        Group(Text("  [magenta]💭[/]  思考中…", style="dim")),
        console=console,
        refresh_per_second=12,
        transient=True,
    ) as live:
        async for chunk in stream_turn(agent, message, thread_id):
            if chunk.startswith("\n[调用工具:"):
                tool_parts.append(chunk.strip())
                thinking_done = True
                live.update(_live_group())
                continue
            if chunk.startswith("\n[工具结果:"):
                tool_results.append(chunk.strip())
                live.update(_live_group())
                continue
            if chunk.startswith("\n[工具错误:"):
                tool_results.append(chunk.strip())
                live.update(_live_group())
                continue
            if not thinking_done:
                thinking_parts.append(chunk)
                live.update(_live_group())
            else:
                response_parts.append(chunk)
                live.update(_live_group())

    # ── Phase 2: render thinking panel + tools + response ────────────────
    if not tool_parts and not thinking_done:
        response_parts = thinking_parts
        thinking_parts = []

    thinking_text = "".join(thinking_parts).strip()
    response_text = "".join(response_parts).strip()

    _print_thinking_panel(thinking_text, fold_state)
    for raw in tool_parts:
        _print_tool_call(raw)
    for raw in tool_results:
        summary = _summarize_tool_result(raw)
        if summary:
            console.print(f"  [dim]  {summary}[/]")
    if response_text:
        console.print()
        console.print(Markdown(response_text))

    # ── Phase 3: fold toggle via Live context ────────────────────────────
    if thinking_text:
        with Live(
            _render_turn(thinking_text, fold_state, tool_parts, response_text),
            console=console,
            refresh_per_second=4,
            transient=False,
        ) as live:
            while True:
                try:
                    key = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: input(),
                    )
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break

                if key.strip().lower() == "t":
                    fold_state = "unfolded" if fold_state == "folded" else "folded"
                    live.update(
                        _render_turn(thinking_text, fold_state, tool_parts, response_text)
                    )
                    continue
                break
    else:
        console.print()

    # ── Persist turn to ChatStore ────────────────────────────────────────
    if chat_store is not None:
        for i, raw in enumerate(tool_parts):
            inner = raw.strip().removeprefix("[调用工具:").removesuffix("]").strip()
            name = inner.split("(")[0].strip() if "(" in inner else inner
            args_part = inner[len(name):].strip()
            tool_output = ""
            if i < len(tool_results):
                tool_output = tool_results[i].removeprefix("[工具结果:").removesuffix("]").strip()
            chat_store.append_message(
                thread_id, "tool", tool_output or args_part,
                tool_name=name, tool_input=args_part,
            )
        if response_text:
            chat_store.append_message(
                thread_id, "assistant", response_text,
                thinking=thinking_text,
            )

    return turn_count + 1, fold_state


# ═══════════════════════════════════════════════════════════════════════════
#  Slash commands
# ═══════════════════════════════════════════════════════════════════════════

async def _handle_slash_command(
    cmd_text: str, agent, thread_id: str,
    turn_count: int, fold_state: str,
    chat_store: ChatStore | None = None,
) -> tuple[int | None, str, str | None]:
    """Handle a slash command. Returns (turn_count_or_None, fold_state, new_thread_id_or_None)."""
    parts = cmd_text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        console.print("\n  [dim]再见 👋[/]\n")
        return None, fold_state, None

    if cmd == "/clear":
        console.clear()
        _show_welcome()
        return turn_count, fold_state, None

    if cmd == "/help":
        _show_help()
        return turn_count, fold_state, None

    if cmd == "/model":
        _show_model_info()
        return turn_count, fold_state, None

    if cmd == "/history":
        console.print(f"\n  [cyan]📊[/]  已进行 [bold]{turn_count}[/] 轮对话\n")
        return turn_count, fold_state, None

    if cmd == "/list":
        tc, fs = await _run_agent_turn(
            agent, "请帮我查看当前所有订阅列表", thread_id, turn_count, fold_state,
            chat_store=chat_store,
        )
        return tc, fs, None

    if cmd == "/subscribe":
        if not arg:
            console.print("  [yellow]用法:[/] /subscribe <游戏名>")
            return turn_count, fold_state, None
        tc, fs = await _run_agent_turn(
            agent, f"我想订阅 {arg} 的价格提醒，请引导我完成订阅",
            thread_id, turn_count, fold_state,
            chat_store=chat_store,
        )
        return tc, fs, None

    if cmd == "/query":
        if not arg:
            console.print("  [yellow]用法:[/] /query <游戏名>")
            return turn_count, fold_state, None
        tc, fs = await _run_agent_turn(
            agent, f"查询 {arg} 的当前价格和历史最低价",
            thread_id, turn_count, fold_state,
            chat_store=chat_store,
        )
        return tc, fs, None

    if cmd == "/recommend":
        if not arg:
            console.print("  [yellow]用法:[/] /recommend <游戏名>")
            return turn_count, fold_state, None
        tc, fs = await _run_agent_turn(
            agent, f"推荐和 {arg} 类似的游戏",
            thread_id, turn_count, fold_state,
            chat_store=chat_store,
        )
        return tc, fs, None

    if cmd == "/export":
        if chat_store is None:
            console.print("  [yellow]对话存储不可用[/]")
            return turn_count, fold_state, None
        fmt = arg.lower() if arg else "md"
        if fmt not in ("md", "json", "html"):
            console.print("  [yellow]格式支持: md, json, html[/]")
            return turn_count, fold_state, None
        ext = {"md": ".md", "json": ".json", "html": ".html"}[fmt]
        filename = f"valveye_{thread_id[:8]}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
        content = getattr(chat_store, f"export_{'markdown' if fmt == 'md' else fmt}")(thread_id)
        out_path = Path.cwd() / filename
        out_path.write_text(content, encoding="utf-8")
        console.print(f"\n  [green]✓[/] 已导出到 [cyan]{out_path}[/]\n")
        return turn_count, fold_state, None

    if cmd == "/resume":
        if chat_store is None:
            console.print("  [yellow]对话存储不可用[/]")
            return turn_count, fold_state, None
        result = _pick_conversation(chat_store)
        if result == "__new__":
            new_id = chat_store.create_thread()
            return 0, "folded", new_id
        if result:
            return 0, "folded", result
        return turn_count, fold_state, None

    if cmd == "/new":
        if chat_store is None:
            return turn_count, fold_state, None
        new_id = chat_store.create_thread()
        console.print("\n  [green]✓[/] 已创建新对话\n")
        return 0, "folded", new_id

    console.print(f"  [yellow]未知命令[/] {cmd}  ·  输入 [cyan]/help[/] 查看可用命令")
    return turn_count, fold_state, None


# ═══════════════════════════════════════════════════════════════════════════
#  Non-chat commands (unchanged logic, minor print polish)
# ═══════════════════════════════════════════════════════════════════════════

def parse_channels_arg(raw_channels: str) -> list[dict]:
    try:
        parsed = json.loads(raw_channels)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--channels 必须是合法 JSON：{exc}") from exc

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("--channels 必须是 JSON 对象或 JSON 数组")

    normalized: list[dict] = []
    for i, item in enumerate(parsed):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--channels 第 {i + 1} 项不是合法 JSON 对象字符串：{exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"--channels 第 {i + 1} 项必须是 JSON 对象")
        normalized.append(item)

    return normalized


def build_services():
    repo = SubscriptionRepository(db_path=settings.subscription_db_path)
    sources = [ITADSource(), SteamDBSource(), CheapSharkSource()]
    price_service = PriceService(sources=sources)
    game_data = GameDataService()
    recommender = Recommender(data_service=game_data)
    notifier = Notifier()
    scheduler = PriceCheckScheduler(repo=repo, price_service=price_service, notifier=notifier)
    tools = build_tools(price_service=price_service, recommender=recommender, game_data=game_data, repo=repo)
    return repo, price_service, recommender, scheduler, tools, game_data, notifier


async def _run(args: argparse.Namespace) -> int:
    repo, price_service, recommender, scheduler, tools, game_data, notifier = build_services()

    if args.command == "query":
        snapshot = await price_service.fetch_first_available(args.game, args.region, args.currency)
        decision = price_service.evaluate_low(snapshot, args.window)
        print(
            json.dumps(
                {
                    "title": snapshot.title,
                    "source": snapshot.source,
                    "current_price": snapshot.current_price,
                    "historical_low": snapshot.historical_low,
                    "currency": snapshot.currency,
                    "is_at_low": decision.is_at_low,
                    "is_new_low": decision.is_new_low,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "recommend":
        rows = await recommender.recommend(args.game, args.top)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.command == "subscribe":
        try:
            channels = parse_channels_arg(args.channels)
        except ValueError as exc:
            print(f"参数错误: {exc}")
            return 2
        sub_id, created = repo.add(
            user_id=args.user,
            game_query=args.game,
            window=args.window,
            region=args.region,
            currency=args.currency,
            channels=channels,
        )
        if created:
            print(f"订阅创建成功: {sub_id}")
        else:
            print(f"订阅已存在: {sub_id}")
        return 0

    if args.command == "list":
        rows = repo.list_active()
        print(
            json.dumps(
                [
                    {
                        "id": r.id,
                        "user_id": r.user_id,
                        "game": r.game_query,
                        "window": r.window,
                        "region": r.region,
                        "currency": r.currency,
                        "channels": r.channels,
                    }
                    for r in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "check-once":
        await scheduler.run_once()
        print("已执行一次订阅价格检测")
        return 0

    if args.command == "scheduler":
        scheduler.start()
        print("调度器已启动，按 Ctrl+C 退出")
        while True:
            await asyncio.sleep(3600)

    # ── chat ──────────────────────────────────────────────────────────────
    if args.command == "chat":
        agent = build_agent_executor(tools)
        chat_store = ChatStore()
        thread_id = chat_store.create_thread()

        try:
            # single-turn mode
            if args.message:
                reply = await run_single_turn(agent, args.message, thread_id)
                console.print(Markdown(reply))
                return 0

            # interactive mode
            _show_welcome()
            turn_count = 0
            fold_state = "folded"
            kb = _build_keybindings()
            toolbar_state = [0.0]  # [0] = hint show timestamp

            def _make_session():
                return PromptSession(
                    history=FileHistory(str(_history_path())),
                    completer=_SlashCommandCompleter(),
                    key_bindings=kb,
                    bottom_toolbar=_build_toolbar(thread_id, turn_count, chat_store, toolbar_state),
                    style=_PROMPT_STYLE,
                )

            session = _make_session()

            while True:
                try:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: session.prompt(
                            [("class:prompt", " ❯ ")],
                        ),
                    )
                except (EOFError, KeyboardInterrupt):
                    console.print("\n  [dim]再见 👋[/]\n")
                    break

                # Double-Esc sentinel
                if user_input == "__switch__":
                    result = _switch_menu(chat_store)
                    if result == "__clear__":
                        console.print("  [dim]输入已清空[/]")
                    elif result == "__new__":
                        thread_id = chat_store.create_thread()
                        turn_count = 0
                        fold_state = "folded"
                        console.print("  [green]✓[/] 已创建新对话")
                    elif result:
                        thread_id = result
                        turn_count = 0
                        fold_state = "folded"
                        thread = chat_store.get_thread(thread_id)
                        title = thread.get("title", "对话") if thread else "对话"
                        console.print(f"  [green]✓[/] 已切换到: [cyan]{title}[/]")
                    if result:
                        toolbar_state[0] = time.monotonic()
                        session = _make_session()
                    console.print()
                    continue

                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.lower() in ("quit", "exit", "q"):
                    console.print("\n  [dim]再见 👋[/]\n")
                    break

                if user_input.startswith("/"):
                    result, fold_state, new_tid = await _handle_slash_command(
                        user_input, agent, thread_id, turn_count, fold_state,
                        chat_store=chat_store,
                    )
                    if result is None:
                        break
                    turn_count = result
                    if new_tid is not None:
                        thread_id = new_tid
                    continue

                # Log user message
                chat_store.append_message(thread_id, "user", user_input)

                turn_count, fold_state = await _run_agent_turn(
                    agent, user_input, thread_id, turn_count, fold_state,
                    chat_store=chat_store,
                )

            return 0
        finally:
            await game_data.close()
            await price_service.close()
            await notifier.close()
            repo.close()

    return 1


# ═══════════════════════════════════════════════════════════════════════════
#  Argument parser
# ═══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valveye Steam Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="查询游戏史低")
    q.add_argument("--game", required=True)
    q.add_argument("--region", default="CN")
    q.add_argument("--currency", default="CNY")
    q.add_argument("--window", default="all", choices=["all", "12m", "3m"])

    r = sub.add_parser("recommend", help="推荐同类游戏")
    r.add_argument("--game", required=True)
    r.add_argument("--top", type=int, default=15)

    s = sub.add_parser("subscribe", help="订阅游戏史低提醒")
    s.add_argument("--user", required=True)
    s.add_argument("--game", required=True)
    s.add_argument("--region", default="CN")
    s.add_argument("--currency", default="CNY")
    s.add_argument("--window", default="all", choices=["all", "12m", "3m"])
    s.add_argument(
        "--channels",
        required=True,
        help='JSON 数组，如: [{"type":"email","to":"you@example.com"}]',
    )

    sub.add_parser("list", help="查看订阅")
    sub.add_parser("check-once", help="立即执行一次检测")
    sub.add_parser("scheduler", help="启动定时检测")

    c = sub.add_parser("chat", help="与 AI 助手对话")
    c.add_argument("--message", "-m", default=None, help="单次查询（不进入交互模式）")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    for warning in settings.validate():
        print(f"⚠ {warning}", file=sys.stderr)

    return asyncio.run(_run(args))
