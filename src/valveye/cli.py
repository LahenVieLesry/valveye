from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from valveye.agent import build_agent_executor, run_single_turn, stream_turn
from valveye.agent_tools import build_tools
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
}


# ═══════════════════════════════════════════════════════════════════════════
#  UI helpers
# ═══════════════════════════════════════════════════════════════════════════

def _build_completer() -> WordCompleter:
    return WordCompleter(list(_SLASH_COMMANDS.keys()), ignore_case=True, sentence=True)


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
    """Format a tool-call marker line:  ⚙ tool_name({…})"""
    # raw is like "\n[调用工具: query_low_price({…})]\n"
    inner = raw.strip().removeprefix("[调用工具:").removesuffix("]").strip()
    name = inner.split("(")[0].strip() if "(" in inner else inner
    args_part = inner[len(name):].strip()
    console.print(f"  [blue]⚙[/]  [dim]{name}[/][dim cyan]{args_part}[/]")


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


def _count_printed_lines(thinking_text: str, tool_parts: list[str], response_text: str) -> int:
    """Estimate how many terminal lines the turn occupies (for cursor repositioning)."""
    width = console.width or 80
    lines = 2  # panel top border + title
    # thinking panel content
    if thinking_text:
        for ln in thinking_text.split("\n"):
            lines += max(1, (len(ln) + 4) // (width - 4) + 1)
        lines += 1  # bottom border
    # tool lines
    lines += len(tool_parts)
    # response
    if response_text:
        for ln in response_text.split("\n"):
            lines += max(1, (len(ln) + 2) // width + 1)
    lines += 2  # prompt line + margin
    return lines


# ═══════════════════════════════════════════════════════════════════════════
#  Agent turn — streaming thinking → fold → response
# ═══════════════════════════════════════════════════════════════════════════

async def _run_agent_turn(
    agent, message: str, thread_id: str,
    turn_count: int, fold_state: str = "folded",
) -> tuple[int, str]:
    """Execute one agent turn.  Returns (new_turn_count, new_fold_state)."""

    thinking_parts: list[str] = []
    response_parts: list[str] = []
    tool_parts: list[str] = []
    thinking_done = False

    # ── Phase 1: live-stream thinking ────────────────────────────────────
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
                continue
            if not thinking_done:
                thinking_parts.append(chunk)
                live.update(
                    Group(
                        Text("  [magenta]💭[/]  思考中…", style="dim"),
                        Text("  " + "".join(thinking_parts)[-300:], style="dim"),
                    )
                )
            else:
                response_parts.append(chunk)

    # ── Phase 2: render thinking panel + tools + response ────────────────
    # If the model answered directly (no tools), treat everything as response
    if not tool_parts and not thinking_done:
        response_parts = thinking_parts
        thinking_parts = []

    thinking_text = "".join(thinking_parts).strip()

    _print_thinking_panel(thinking_text, fold_state)

    for raw in tool_parts:
        _print_tool_call(raw)

    response_text = "".join(response_parts).strip()
    if response_text:
        console.print()
        console.print(Markdown(response_text))

    # ── Phase 3: fold toggle ─────────────────────────────────────────────
    if thinking_text:
        console.print()
        while True:
            try:
                key = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: input("  \033[2m(T) 展开/折叠 · (Enter) 继续\033[0m  "),
                )
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            if key.strip().lower() == "t":
                # clear the turn and redraw
                n = _count_printed_lines(thinking_text, tool_parts, response_text)
                sys.stdout.write(f"\033[{n}A\033[0J")
                sys.stdout.flush()
                fold_state = "unfolded" if fold_state == "folded" else "folded"
                _print_thinking_panel(thinking_text, fold_state)
                for raw in tool_parts:
                    _print_tool_call(raw)
                if response_text:
                    console.print()
                    console.print(Markdown(response_text))
                console.print()
                continue
            break
    else:
        console.print()

    return turn_count + 1, fold_state


# ═══════════════════════════════════════════════════════════════════════════
#  Slash commands
# ═══════════════════════════════════════════════════════════════════════════

async def _handle_slash_command(
    cmd_text: str, agent, thread_id: str,
    turn_count: int, fold_state: str,
) -> tuple[int | None, str]:
    parts = cmd_text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        console.print("\n  [dim]再见 👋[/]\n")
        return None, fold_state

    if cmd == "/clear":
        console.clear()
        _show_welcome()
        return turn_count, fold_state

    if cmd == "/help":
        _show_help()
        return turn_count, fold_state

    if cmd == "/model":
        _show_model_info()
        return turn_count, fold_state

    if cmd == "/history":
        console.print(f"\n  [cyan]📊[/]  已进行 [bold]{turn_count}[/] 轮对话\n")
        return turn_count, fold_state

    if cmd == "/list":
        return await _run_agent_turn(
            agent, "请帮我查看当前所有订阅列表", thread_id, turn_count, fold_state,
        )

    if cmd == "/subscribe":
        if not arg:
            console.print("  [yellow]用法:[/] /subscribe <游戏名>")
            return turn_count, fold_state
        return await _run_agent_turn(
            agent, f"我想订阅 {arg} 的价格提醒，请引导我完成订阅",
            thread_id, turn_count, fold_state,
        )

    if cmd == "/query":
        if not arg:
            console.print("  [yellow]用法:[/] /query <游戏名>")
            return turn_count, fold_state
        return await _run_agent_turn(
            agent, f"查询 {arg} 的当前价格和历史最低价",
            thread_id, turn_count, fold_state,
        )

    if cmd == "/recommend":
        if not arg:
            console.print("  [yellow]用法:[/] /recommend <游戏名>")
            return turn_count, fold_state
        return await _run_agent_turn(
            agent, f"推荐和 {arg} 类似的游戏",
            thread_id, turn_count, fold_state,
        )

    console.print(f"  [yellow]未知命令[/] {cmd}  ·  输入 [cyan]/help[/] 查看可用命令")
    return turn_count, fold_state


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
    repo = SubscriptionRepository(db_path=settings.sqlite_path)
    sources = [ITADSource(), SteamDBSource(), CheapSharkSource()]
    price_service = PriceService(sources=sources)
    game_data = GameDataService()
    recommender = Recommender(data_service=game_data)
    notifier = Notifier()
    scheduler = PriceCheckScheduler(repo=repo, price_service=price_service, notifier=notifier)
    tools = build_tools(price_service=price_service, recommender=recommender, game_data=game_data, repo=repo)
    return repo, price_service, recommender, scheduler, tools, game_data


async def _run(args: argparse.Namespace) -> int:
    repo, price_service, recommender, scheduler, tools, game_data = build_services()

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
        thread_id = str(uuid.uuid4())

        try:
            # single-turn mode
            if args.message:
                reply = await run_single_turn(agent, args.message, thread_id)
                console.print(Markdown(reply))
                return 0

            # interactive mode
            _show_welcome()
            session = PromptSession(history=InMemoryHistory(), completer=_build_completer())
            turn_count = 0
            fold_state = "folded"

            while True:
                try:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: session.prompt(
                            [("class:prompt", " ❯ ")],
                            style=Style.from_dict({"prompt": "bold cyan"}),
                        ),
                    )
                except (EOFError, KeyboardInterrupt):
                    console.print("\n  [dim]再见 👋[/]\n")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.lower() in ("quit", "exit", "q"):
                    console.print("\n  [dim]再见 👋[/]\n")
                    break

                if user_input.startswith("/"):
                    result, fold_state = await _handle_slash_command(
                        user_input, agent, thread_id, turn_count, fold_state,
                    )
                    if result is None:
                        break
                    turn_count = result
                    continue

                turn_count, fold_state = await _run_agent_turn(
                    agent, user_input, thread_id, turn_count, fold_state,
                )

            return 0
        finally:
            await game_data.close()

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
    return asyncio.run(_run(args))
