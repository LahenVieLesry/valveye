from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import signal
import subprocess
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

from valveye.agent import build_multi_agent, run_single_turn, stream_turn
from valveye.agent_tools import build_tools
from valveye.chat_store import ChatStore
from valveye.config import settings
from valveye.data_sources.cheapshark import CheapSharkSource
from valveye.data_sources.itad import ITADSource
from valveye.data_sources.steamdb import SteamDBSource
from valveye.formatter import build_notification
from valveye.game_data import GameDataService
from valveye.memory import VikingMemory
from valveye.notifications import Notifier
from valveye.pricing import PriceService
from valveye.recommendation import Recommender
from valveye.scheduler import PriceCheckScheduler
from valveye.steam_library import SteamLibraryService
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
    "request_game_details":   "请求游戏详情",
    "get_player_library":     "查看游戏库",
}

_AGENT_DISPLAY: dict[str, str] = {
    "price_agent":      "价格查询",
    "info_agent":       "游戏信息",
    "recommend_agent":  "游戏推荐",
    "subs_agent":       "订阅管理",
    "direct":           "助手",
}


# ═══════════════════════════════════════════════════════════════════════════
#  UI helpers
# ═══════════════════════════════════════════════════════════════════════════

class _SlashCommandCompleter(Completer):
    """Completer that shows a command palette with descriptions when typing /."""

    def get_completions(self, document, complete_event):
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


def _build_keybindings(deals_state: list | None = None) -> KeyBindings:
    """Build custom key bindings: double-Esc switch, cursor-aware up/down, / trigger, Ctrl+D deals."""
    kb = KeyBindings()
    _last_esc_time = 0.0
    _saved_text: str | None = None  # stores text cleared by Down for Up to restore

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
        nonlocal _saved_text
        buf = event.current_buffer
        if buf.complete_state is not None:
            buf.complete_previous()
            return
        doc = buf.document
        # Multi-line: not on first line → move cursor up one line
        if doc.cursor_position_row > 0:
            buf.cursor_up()
            return
        # First line, not at beginning → go to beginning
        if doc.cursor_position_col > 0:
            buf.cursor_position = 0
            return
        # At beginning of first line → history backward, restore saved text if available
        if _saved_text is not None:
            buf.text = _saved_text
            buf.cursor_position = len(_saved_text)
            _saved_text = None
            return
        buf.history_backward()
        buf.cursor_position = len(buf.text)

    @kb.add("down")
    def _(event):
        nonlocal _saved_text
        buf = event.current_buffer
        if buf.complete_state is not None:
            buf.complete_next()
            return
        doc = buf.document
        last_row = doc.line_count - 1
        # Multi-line: not on last line → move cursor down one line
        if doc.cursor_position_row < last_row:
            buf.cursor_down()
            return
        # Last line, not at end → go to end
        if doc.cursor_position_col < len(doc.current_line):
            buf.cursor_position = len(buf.text)
            return
        # At end of last line → history forward; if no more history, clear and save
        if buf.history_forward():
            buf.cursor_position = len(buf.text)
        else:
            if buf.text:
                _saved_text = buf.text
            buf.reset()

    @kb.add("/")
    def _(event):
        buffer = event.app.current_buffer
        buffer.insert_text("/")
        if buffer.text.startswith("/"):
            buffer.start_completion(select_first=False)

    @kb.add("c-d", eager=True)
    def _(event):
        if deals_state is not None:
            deal_result = deals_state[1]
            deal_ts = deals_state[2]
            if deal_result is not None and deal_ts and time.monotonic() - deal_ts < 10:
                event.app.exit(result="__deals__")

    return kb


def _read_key() -> str:
    """Read a single keypress, returning escape sequences for special keys."""
    import termios
    import tty
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


def _build_toolbar(thread_id: str, turn_count_ref: list[int], chat_store: ChatStore, state: list):
    """Build a bottom_toolbar callable for PromptSession.

    turn_count_ref: mutable list where [0] holds the current turn count.
    state[0] = timestamp when hint should be shown (0 = no hint).
    state[1] = _StartupDealResult or None (startup deal check result).
    state[2] = timestamp when deal result was set (0 = no result).
    """
    def _toolbar():
        thread = chat_store.get_thread(thread_id)
        title = (thread.get("title", "新对话") if thread else "新对话")[:30]
        model = settings.openai_model
        parts = [
            ("class:toolbar", f" {title} "),
            ("class:toolbar", f" · {model} "),
            ("class:toolbar", f" · 第{turn_count_ref[0]}轮 "),
        ]

        # Show deal check result in toolbar for 10 seconds
        deal_result = state[1]
        deal_ts = state[2]
        if deal_result is not None and deal_ts and time.monotonic() - deal_ts < 10:
            if deal_result.at_low > 0 or deal_result.new_low > 0:
                parts.append((
                    "class:toolbar-deals",
                    f" · {deal_result.total}个游戏中 {deal_result.at_low}个史低 {deal_result.new_low}个新史低 ",
                ))
            else:
                parts.append(("class:toolbar", f" · {deal_result.total}个游戏无优惠 "))
            parts.append(("class:toolbar-dim", " Ctrl+D查看详情"))

        if state[0] and time.monotonic() - state[0] < 2.5:
            parts.append(("class:toolbar-dim", " · EscEsc切换对话"))
        return parts
    return _toolbar


_PROMPT_STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
    "toolbar": "bold ansiblack bg:ansicyan",
    "toolbar-dim": "ansiblack bg:ansicyan",
    "toolbar-deals": "bold ansiblack bg:ansigreen",
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


@dataclasses.dataclass
class _StartupDealResult:
    total: int = 0
    at_low: int = 0
    new_low: int = 0
    deals: list[tuple[str, str, float, float, str, bool]] = dataclasses.field(default_factory=list)
    # (title, currency, current_price, historical_low, source, is_new_low)


async def _run_startup_deal_check(
    repo: SubscriptionRepository,
    price_service: PriceService,
    notifier: Notifier,
    game_data_service: GameDataService | None = None,
) -> _StartupDealResult:
    """Check all subscribed games for deals on session startup.

    Only sends notifications for new deals (price dropped since last notified).
    All deals are collected for display in the results table.
    """
    result = _StartupDealResult()
    subs = repo.list_active()
    if not subs:
        return result

    result.total = len(subs)
    for sub in subs:
        try:
            snapshot = await price_service.fetch_first_available(
                game_query=sub.game_query,
                region=sub.region,
                currency=sub.currency,
            )
            decision = price_service.evaluate_low(
                snapshot=snapshot,
                window=sub.window,
                known_notified_low=sub.last_notified_low,
            )
        except Exception:
            continue

        if not (decision.is_at_low or decision.is_new_low):
            continue

        if decision.is_new_low:
            result.new_low += 1
        elif decision.is_at_low:
            result.at_low += 1

        result.deals.append((
            snapshot.title,
            snapshot.currency,
            snapshot.current_price,
            snapshot.historical_low,
            snapshot.source,
            decision.is_new_low,
        ))

        # Only notify for new deals (price dropped since last notification)
        if decision.is_new_low:
            tag = "新史低"

            profile = None
            if snapshot.app_id and game_data_service:
                try:
                    profile = await game_data_service.fetch_profile(snapshot.app_id)
                except Exception:
                    profile = None

            msg = build_notification(snapshot, tag, sub.window, profile)
            for ch in sub.channels:
                try:
                    channel = ch if isinstance(ch, dict) else json.loads(ch)
                    await notifier.send(channel=channel, message=msg)
                except Exception:
                    pass
            repo.mark_notified(sub.id, decision.window_low)

    return result


def _show_deals_table(result: _StartupDealResult) -> None:
    """Show deals in a Rich table. Blocks until user presses Esc."""
    if not result.deals:
        return

    t = Table(
        show_header=True, header_style="bold", show_lines=False,
        pad_edge=False, padding=(0, 2),
    )
    t.add_column("游戏", style="cyan", min_width=24)
    t.add_column("当前价", justify="right", min_width=10)
    t.add_column("史低价", justify="right", min_width=10)
    t.add_column("来源", style="dim", min_width=8)
    t.add_column("状态", min_width=8)

    for title, currency, cur, low, source, is_new in result.deals:
        status = Text("新史低", style="bold red") if is_new else Text("史低", style="yellow")
        t.add_row(
            title[:40],
            f"{cur:.2f} {currency}",
            f"{low:.2f} {currency}",
            source,
            status,
        )

    console.print()
    console.print(
        Panel(
            t,
            title=f"[bold]优惠结果[/]  {result.total}个游戏 · {result.at_low}个史低 · {result.new_low}个新史低",
            border_style="green",
            padding=(0, 1),
        )
    )
    console.print("  [dim]按 Esc 返回对话[/]")

    # Block until Esc
    while True:
        key = _read_key()
        if key == "escape":
            break


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


def _render_turn(thinking_text: str, fold_state: str, tool_calls: list[dict], response_text: str) -> Group:
    """Build a Rich renderable for the full agent turn."""
    items = []
    if thinking_text:
        items.append(_build_thinking_panel(thinking_text, fold_state))
    for tc in tool_calls:
        display_name = _TOOL_DISPLAY.get(tc["name"], tc["name"])
        game = tc.get("inputs", {}).get("game", "") if tc.get("inputs") else ""
        if game:
            items.append(Text(f"  ⚙  {display_name} → {game}", style="dim"))
        else:
            items.append(Text(f"  ⚙  {display_name}", style="dim"))
        if tc.get("output"):
            summary = _summarize_tool_result(tc["output"])
            if summary:
                items.append(Text(f"    {summary}", style="dim"))
    if response_text:
        items.append(Text(""))
        items.append(Markdown(response_text))
    items.append(Text(""))
    items.append(Text("  (T) 展开/折叠 · (Enter) 继续", style="dim"))
    return Group(*items)


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
    memory: VikingMemory | None = None,
) -> tuple[int, str]:
    """Execute one agent turn.  Returns (new_turn_count, new_fold_state)."""

    thinking_parts: list[str] = []
    response_parts: list[str] = []
    tool_calls: list[dict] = []    # {"id": ..., "name": ..., "inputs": ..., "output": ...}
    thinking_done = False
    _tool_id_counter = 0
    current_agent_label = ""

    def _live_group() -> Group:
        items: list = []
        if current_agent_label:
            items.append(Text(f"  [bold cyan]▸[/] {current_agent_label}"))
        if not thinking_done:
            items.append(Text("  [magenta]💭[/]  思考中…", style="dim"))
            items.append(Text("  " + "".join(thinking_parts)[-300:], style="dim"))
        for tc in tool_calls:
            display_name = _TOOL_DISPLAY.get(tc["name"], tc["name"])
            game = tc["inputs"].get("game", "") if tc["inputs"] else ""
            if game:
                items.append(Text(f"  [blue]⚙[/]  {display_name} → {game}", style="dim"))
            else:
                items.append(Text(f"  [blue]⚙[/]  {display_name}", style="dim"))
            if tc.get("output"):
                summary = _summarize_tool_result(tc["output"])
                if summary:
                    items.append(Text(f"  [dim]    {summary}[/]"))
        if response_parts:
            items.append(Markdown("".join(response_parts)))
        return Group(*items)

    # ── Phase 1: live-stream ─────────────────────────────────────────────
    with Live(
        Group(Text("  [magenta]💭[/]  思考中…", style="dim")),
        console=console,
        refresh_per_second=12,
        transient=True,
    ) as live:
        async for event in stream_turn(agent, message, thread_id, memory=memory):
            etype = event["type"]

            if etype == "agent_start":
                agent_name = event["agent"]
                current_agent_label = _AGENT_DISPLAY.get(agent_name, agent_name)
                live.update(_live_group())

            elif etype == "handoff":
                from_label = _AGENT_DISPLAY.get(event["from"], event["from"])
                to_label = _AGENT_DISPLAY.get(event["to"], event["to"])
                # Show handoff in live view
                items = _live_group().renderables
                items.append(Text(f"  [dim]↗ {from_label} → {to_label}[/]"))
                live.update(Group(*items))

            elif etype == "token":
                if not thinking_done:
                    thinking_parts.append(event["content"])
                else:
                    response_parts.append(event["content"])
                live.update(_live_group())

            elif etype == "tool_start":
                thinking_done = True
                _tool_id_counter += 1
                tool_calls.append({
                    "id": _tool_id_counter,
                    "name": event["name"],
                    "inputs": event.get("inputs", {}),
                    "output": "",
                })
                live.update(_live_group())

            elif etype == "tool_end":
                # Find the first unmatched tool call with this name
                for tc in tool_calls:
                    if tc["name"] == event["name"] and not tc["output"]:
                        tc["output"] = event.get("output", "")
                        break
                live.update(_live_group())

            elif etype == "agent_end":
                live.update(_live_group())

    # ── Phase 2: render final result ─────────────────────────────────────
    # If no tool calls were made, treat thinking as response
    if not tool_calls and not thinking_done:
        response_parts = thinking_parts
        thinking_parts = []

    thinking_text = "".join(thinking_parts).strip()
    response_text = "".join(response_parts).strip()

    # Print agent header only when agent did actual work (tools or thinking)
    if current_agent_label and (tool_calls or thinking_text):
        console.print(f"\n  [bold cyan]▸[/] {current_agent_label}")

    _print_thinking_panel(thinking_text, fold_state)
    for tc in tool_calls:
        display_name = _TOOL_DISPLAY.get(tc["name"], tc["name"])
        game = tc["inputs"].get("game", "") if tc["inputs"] else ""
        if game:
            console.print(f"  [blue]⚙[/]  [dim]{display_name}[/] [dim cyan]→ {game}[/]")
        else:
            console.print(f"  [blue]⚙[/]  [dim]{display_name}[/]")
        if tc.get("output"):
            summary = _summarize_tool_result(tc["output"])
            if summary:
                console.print(f"  [dim]    {summary}[/]")
    if response_text:
        console.print()
        console.print(Markdown(response_text))

    # ── Phase 3: fold toggle ─────────────────────────────────────────────
    if thinking_text:
        with Live(
            _render_turn(thinking_text, fold_state, tool_calls, response_text),
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
                        _render_turn(thinking_text, fold_state, tool_calls, response_text)
                    )
                    continue
                break
    else:
        console.print()

    # ── Persist turn to ChatStore ────────────────────────────────────────
    if chat_store is not None:
        for tc in tool_calls:
            tool_output = tc.get("output", "")
            inputs = tc.get("inputs", {})
            args_str = ", ".join(f"{k}={v}" for k, v in inputs.items() if v) if inputs else ""
            chat_store.append_message(
                thread_id, "tool", tool_output or args_str,
                tool_name=tc["name"], tool_input=args_str,
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
    memory: VikingMemory | None = None,
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
            chat_store=chat_store, memory=memory,
        )
        return tc, fs, None

    if cmd == "/subscribe":
        if not arg:
            console.print("  [yellow]用法:[/] /subscribe <游戏名>")
            return turn_count, fold_state, None
        tc, fs = await _run_agent_turn(
            agent, f"我想订阅 {arg} 的价格提醒，请引导我完成订阅",
            thread_id, turn_count, fold_state,
            chat_store=chat_store, memory=memory,
        )
        return tc, fs, None

    if cmd == "/query":
        if not arg:
            console.print("  [yellow]用法:[/] /query <游戏名>")
            return turn_count, fold_state, None
        tc, fs = await _run_agent_turn(
            agent, f"查询 {arg} 的当前价格和历史最低价",
            thread_id, turn_count, fold_state,
            chat_store=chat_store, memory=memory,
        )
        return tc, fs, None

    if cmd == "/recommend":
        if not arg:
            console.print("  [yellow]用法:[/] /recommend <游戏名>")
            return turn_count, fold_state, None
        tc, fs = await _run_agent_turn(
            agent, f"推荐和 {arg} 类似的游戏",
            thread_id, turn_count, fold_state,
            chat_store=chat_store, memory=memory,
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


# ═══════════════════════════════════════════════════════════════════════════
#  OpenViking Server lifecycle management
# ═══════════════════════════════════════════════════════════════════════════

_OV_SERVER_PROC: subprocess.Popen | None = None


async def _is_openviking_running() -> bool:
    """Check if OpenViking server is already running via health endpoint."""
    import aiohttp
    url = settings.openviking_url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session, session.get(
            f"{url}/health",
            timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_openviking_server() -> subprocess.Popen | None:
    """Start openviking-server as a background subprocess."""
    global _OV_SERVER_PROC
    python_bin = sys.executable
    try:
        popen_kwargs: dict = {}
        if sys.platform != "win32":
            popen_kwargs["preexec_fn"] = os.setsid  # new process group for clean shutdown
        proc = subprocess.Popen(
            [python_bin, "-m", "openviking.server.bootstrap"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
        _OV_SERVER_PROC = proc
        return proc
    except Exception as exc:
        console.print(f"  [yellow]⚠[/] 启动 OpenViking 失败: {exc}")
        return None


async def _stop_openviking_server() -> None:
    """Stop the openviking-server subprocess if we started it."""
    global _OV_SERVER_PROC
    if _OV_SERVER_PROC is None:
        return
    proc = _OV_SERVER_PROC
    _OV_SERVER_PROC = None
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=5)
    except ProcessLookupError:
        pass
    except (asyncio.TimeoutError, subprocess.TimeoutExpired):
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    except Exception:
        pass


def build_services():
    repo = SubscriptionRepository(db_path=settings.subscription_db_path)
    sources = [ITADSource(), SteamDBSource(), CheapSharkSource()]
    price_service = PriceService(sources=sources)
    game_data = GameDataService()
    steam_library = SteamLibraryService(
        steam_api_key=settings.steam_api_key,
        default_steam_id=settings.steam_id,
    )
    recommender = Recommender(data_service=game_data)
    notifier = Notifier()
    scheduler = PriceCheckScheduler(repo=repo, price_service=price_service, notifier=notifier, game_data_service=game_data)
    tools = build_tools(price_service=price_service, recommender=recommender, game_data=game_data, repo=repo, steam_library=steam_library)
    return repo, price_service, recommender, scheduler, tools, game_data, notifier, steam_library


async def _run(args: argparse.Namespace) -> int:
    repo, price_service, recommender, scheduler, tools, game_data, notifier, steam_library = build_services()

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
        all_tools, tool_groups = tools
        chat_store = ChatStore()
        thread_id = chat_store.create_thread()

        # OpenViking 记忆层初始化（自动启动 server）
        # 必须在 build_multi_agent 之前完成，以便 memory 作为闭包传入图节点
        memory: VikingMemory | None = None
        _ov_started_by_us = False
        if settings.openviking_enabled:
            # 检查 server 是否已在运行
            if not await _is_openviking_running():
                console.print("  [dim]🚀 正在启动 OpenViking Server…[/]")
                proc = _start_openviking_server()
                if proc:
                    _ov_started_by_us = True
                    # 等待 server 就绪（最多 15 秒）
                    for _ in range(30):
                        await asyncio.sleep(0.5)
                        if await _is_openviking_running():
                            break
                    else:
                        console.print("  [yellow]⚠[/] OpenViking Server 启动超时")
                        await _stop_openviking_server()
                        _ov_started_by_us = False

            memory = VikingMemory()
            healthy = await memory.health_check()
            if healthy:
                await memory.ensure_dirs()
                await memory.create_session(thread_id)
                console.print("  [green]✓[/] OpenViking 记忆层已启用")
            else:
                console.print("  [yellow]⚠[/] OpenViking 服务不可用，记忆层已禁用")
                await memory.close()
                memory = None
                if _ov_started_by_us:
                    await _stop_openviking_server()
                    _ov_started_by_us = False

        checkpointer_conn = None
        agent, checkpointer_conn = await build_multi_agent(
            tool_groups,
            get_game_details_fn=tool_groups["info"][0],
            memory=memory,
        )

        try:
            # single-turn mode
            if args.message:
                reply = await run_single_turn(agent, args.message, thread_id, memory=memory)
                console.print(Markdown(reply))
                return 0

            # interactive mode
            _show_welcome()
            turn_count_ref = [0]
            fold_state = "folded"
            # [0] = hint show timestamp, [1] = deal result, [2] = deal result timestamp
            toolbar_state = [0.0, None, 0.0]
            deals_state = toolbar_state  # alias for keybinding closure
            kb = _build_keybindings(deals_state)

            def _make_session():
                return PromptSession(
                    history=FileHistory(str(_history_path())),
                    completer=_SlashCommandCompleter(),
                    key_bindings=kb,
                    bottom_toolbar=_build_toolbar(thread_id, turn_count_ref, chat_store, toolbar_state),
                    style=_PROMPT_STYLE,
                )

            # Start background deal check on session startup
            async def _startup_check():
                try:
                    console.print("  [dim]🔍 正在检查订阅游戏优惠…[/]")
                    result = await _run_startup_deal_check(repo, price_service, notifier, game_data)
                    if result.total == 0:
                        console.print("  [green]✓[/] 暂未订阅游戏！")
                        return
                    total_deals = result.at_low + result.new_low
                    if total_deals > 0:
                        console.print(
                            f" [green]✓[/] {result.total}个游戏中"
                            f" [bold]{result.at_low}[/]个史低"
                            f" [bold red]{result.new_low}[/]个新史低"
                            "  [dim]Ctrl+D 查看详情[/]"
                        )
                    else:
                        console.print(f"  [green]✓[/] {result.total}个游戏无优惠")
                    toolbar_state[1] = result
                    toolbar_state[2] = time.monotonic()
                except Exception as e:
                    console.print(f"  [yellow]⚠ 优惠检查失败: {e}[/]")

            asyncio.create_task(_startup_check())

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

                # Deals shortcut sentinel (Ctrl+D)
                if user_input == "__deals__":
                    deal_result = toolbar_state[1]
                    if deal_result and deal_result.deals:
                        _show_deals_table(deal_result)
                    continue

                # Double-Esc sentinel
                if user_input == "__switch__":
                    result = _switch_menu(chat_store)
                    if result == "__clear__":
                        console.print("  [dim]输入已清空[/]")
                    elif result == "__new__":
                        thread_id = chat_store.create_thread()
                        turn_count_ref[0] = 0
                        fold_state = "folded"
                        if memory:
                            await memory.create_session(thread_id)
                        console.print("  [green]✓[/] 已创建新对话")
                    elif result:
                        thread_id = result
                        turn_count_ref[0] = 0
                        fold_state = "folded"
                        if memory:
                            await memory.create_session(thread_id)
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
                        user_input, agent, thread_id, turn_count_ref[0], fold_state,
                        chat_store=chat_store, memory=memory,
                    )
                    if result is None:
                        break
                    turn_count_ref[0] = result
                    if new_tid is not None:
                        thread_id = new_tid
                    continue

                # Log user message
                chat_store.append_message(thread_id, "user", user_input)

                turn_count_ref[0], fold_state = await _run_agent_turn(
                    agent, user_input, thread_id, turn_count_ref[0], fold_state,
                    chat_store=chat_store, memory=memory,
                )

            return 0
        finally:
            if memory:
                await memory.close()
            if _ov_started_by_us:
                console.print("  [dim]⏹ 正在关闭 OpenViking Server…[/]")
                await _stop_openviking_server()
            if checkpointer_conn is not None:
                await checkpointer_conn.close()
            await game_data.close()
            await steam_library.close()
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
