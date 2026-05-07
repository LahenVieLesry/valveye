from __future__ import annotations

import re
from dataclasses import dataclass

from valveye.domain import GameProfile, PriceSnapshot


@dataclass(slots=True)
class NotificationMessage:
    title: str
    plain_text: str
    steam_url: str | None
    thumbnail: str | None
    profile: GameProfile | None
    snapshot: PriceSnapshot
    tag: str
    window: str


def build_notification(
    snapshot: PriceSnapshot,
    tag: str,
    window: str,
    profile: GameProfile | None = None,
) -> NotificationMessage:
    title = f"【{tag}】{snapshot.title}"
    plain_text = _build_plain_text(snapshot, tag, window)

    steam_url: str | None = None
    thumbnail: str | None = None
    app_id = profile.app_id if profile else snapshot.app_id
    if app_id:
        steam_url = f"https://store.steampowered.com/app/{app_id}"
    if profile:
        thumbnail = profile.thumb

    return NotificationMessage(
        title=title,
        plain_text=plain_text,
        steam_url=steam_url,
        thumbnail=thumbnail,
        profile=profile,
        snapshot=snapshot,
        tag=tag,
        window=window,
    )


# ---------------------------------------------------------------------------
# Channel renderers
# ---------------------------------------------------------------------------

def render_plain(msg: NotificationMessage) -> str:
    return msg.plain_text


def render_telegram(msg: NotificationMessage) -> str:
    esc = _escape_telegram_md
    lines: list[str] = []

    lines.append(f"*{esc(msg.title)}*")
    lines.append("")

    discount = _calc_discount(msg.snapshot)
    discount_str = f" \\({_esc_inline(discount)}\\)" if discount else ""
    lines.append(f"💰 当前价: *{_esc_inline(f'{msg.snapshot.current_price:.2f} {msg.snapshot.currency}')}{discount_str}*")
    low_date = ""
    if msg.snapshot.historical_low_at:
        low_date = f" \\({_esc_inline(msg.snapshot.historical_low_at.strftime('%Y-%m-%d'))}\\)"
    lines.append(f"📉 史低价: {_esc_inline(f'{msg.snapshot.historical_low:.2f} {msg.snapshot.currency}')}{low_date}")

    if msg.profile:
        if msg.snapshot.store:
            lines.append(f"🏪 商店: {esc(msg.snapshot.store)}")
        if msg.profile.developer:
            lines.append(f"👨‍💻 开发商: {esc(msg.profile.developer)}")
        if msg.profile.publisher and msg.profile.publisher != msg.profile.developer:
            lines.append(f"🏢 发行商: {esc(msg.profile.publisher)}")
        review = _format_review_score(msg.profile)
        if review:
            lines.append(f"📊 评测: {esc(review)}")
        tags = _format_tags(msg.profile)
        if tags:
            lines.append(f"🏷️ {esc(tags)}")
        platforms = _format_platforms(msg.profile)
        if platforms:
            lines.append(f"🖥️ {esc(platforms)}")
    elif msg.snapshot.store:
        lines.append(f"🏪 商店: {esc(msg.snapshot.store)}")

    lines.append("")
    lines.append(f"📅 口径: {esc(msg.window)} ・ 来源: {esc(msg.snapshot.source)}")

    if msg.steam_url:
        lines.append("")
        lines.append(f"[Steam Store]({msg.steam_url})")

    return "\n".join(lines)


def render_discord_embed(msg: NotificationMessage) -> dict:
    discount = _calc_discount(msg.snapshot)
    color = 0x2ECC71 if msg.tag == "新史低" else 0xF1C40F

    fields: list[dict] = []
    price_value = f"**{msg.snapshot.current_price:.2f} {msg.snapshot.currency}**"
    if discount:
        price_value += f" (-{discount})"
    fields.append({"name": "当前价", "value": price_value, "inline": True})

    low_value = f"{msg.snapshot.historical_low:.2f} {msg.snapshot.currency}"
    if msg.snapshot.historical_low_at:
        low_value += f"\n{msg.snapshot.historical_low_at.strftime('%Y-%m-%d')}"
    fields.append({"name": "史低价", "value": low_value, "inline": True})

    if msg.snapshot.store:
        fields.append({"name": "商店", "value": msg.snapshot.store, "inline": True})

    if msg.profile:
        if msg.profile.developer:
            fields.append({"name": "开发商", "value": msg.profile.developer, "inline": True})
        if msg.profile.publisher and msg.profile.publisher != msg.profile.developer:
            fields.append({"name": "发行商", "value": msg.profile.publisher, "inline": True})
        review = _format_review_score(msg.profile)
        if review:
            fields.append({"name": "评测", "value": review, "inline": True})
        tags = _format_tags(msg.profile)
        if tags:
            fields.append({"name": "标签", "value": tags, "inline": False})
        platforms = _format_platforms(msg.profile)
        if platforms:
            fields.append({"name": "平台", "value": platforms, "inline": True})

    fields.append({"name": "口径", "value": msg.window, "inline": True})

    embed: dict = {
        "title": msg.title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"来源: {msg.snapshot.source}"},
    }
    if msg.steam_url:
        embed["url"] = msg.steam_url
    if msg.thumbnail:
        embed["thumbnail"] = {"url": msg.thumbnail}
    if msg.profile and msg.profile.description:
        embed["description"] = _truncate(msg.profile.description, 200)

    return embed


def render_email_html(msg: NotificationMessage) -> str:
    discount = _calc_discount(msg.snapshot)
    is_new = msg.tag == "新史低"
    accent = "#2ecc71" if is_new else "#f1c40f"

    parts: list[str] = []
    parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>")
    parts.append(f"<div style='max-width:600px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#333;'>")

    if msg.thumbnail:
        parts.append(f"<img src='{msg.thumbnail}' style='width:100%;border-radius:8px 8px 0 0;' alt='cover'>")

    parts.append(f"<div style='padding:20px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px;'>")
    parts.append(f"<h2 style='margin:0 0 8px;color:{accent};'>{_esc_html(msg.title)}</h2>")

    if msg.profile and msg.profile.description:
        parts.append(f"<p style='color:#666;font-size:14px;margin:0 0 16px;'>{_esc_html(_truncate(msg.profile.description, 200))}</p>")

    parts.append("<table style='width:100%;border-collapse:collapse;margin-bottom:16px;'>")
    discount_html = f' <span style="color:#999;">(-{discount})</span>' if discount else ""
    parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>当前价</td>"
                 f"<td style='padding:8px;border-bottom:1px solid #eee;font-size:18px;color:{accent};'>"
                 f"<strong>{msg.snapshot.current_price:.2f} {msg.snapshot.currency}</strong>"
                 f"{discount_html}</td></tr>")
    low_date = ""
    if msg.snapshot.historical_low_at:
        low_date = f" <span style='color:#999;'>({msg.snapshot.historical_low_at.strftime('%Y-%m-%d')})</span>"
    parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>史低价</td>"
                 f"<td style='padding:8px;border-bottom:1px solid #eee;'>{msg.snapshot.historical_low:.2f} {msg.snapshot.currency}{low_date}</td></tr>")

    if msg.snapshot.store:
        parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>商店</td>"
                     f"<td style='padding:8px;border-bottom:1px solid #eee;'>{_esc_html(msg.snapshot.store)}</td></tr>")

    if msg.profile:
        if msg.profile.developer:
            parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>开发商</td>"
                         f"<td style='padding:8px;border-bottom:1px solid #eee;'>{_esc_html(msg.profile.developer)}</td></tr>")
        if msg.profile.publisher and msg.profile.publisher != msg.profile.developer:
            parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>发行商</td>"
                         f"<td style='padding:8px;border-bottom:1px solid #eee;'>{_esc_html(msg.profile.publisher)}</td></tr>")
        review = _format_review_score(msg.profile)
        if review:
            parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>评测</td>"
                         f"<td style='padding:8px;border-bottom:1px solid #eee;'>{_esc_html(review)}</td></tr>")
        tags = _format_tags(msg.profile)
        if tags:
            parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>标签</td>"
                         f"<td style='padding:8px;border-bottom:1px solid #eee;'>{_esc_html(tags)}</td></tr>")
        platforms = _format_platforms(msg.profile)
        if platforms:
            parts.append(f"<tr><td style='padding:8px;border-bottom:1px solid #eee;font-weight:bold;'>平台</td>"
                         f"<td style='padding:8px;border-bottom:1px solid #eee;'>{_esc_html(platforms)}</td></tr>")

    parts.append(f"<tr><td style='padding:8px;font-weight:bold;'>口径</td>"
                 f"<td style='padding:8px;'>{msg.window}</td></tr>")
    parts.append("</table>")

    if msg.steam_url:
        parts.append(f"<a href='{msg.steam_url}' style='display:inline-block;padding:12px 24px;background:{accent};"
                     f"color:#fff;text-decoration:none;border-radius:6px;font-weight:bold;'>在 Steam 查看</a>")

    parts.append(f"<p style='margin-top:16px;font-size:12px;color:#999;'>来源: {msg.snapshot.source}</p>")
    parts.append("</div></div></body></html>")

    return "".join(parts)


def render_webhook_markdown(msg: NotificationMessage) -> str:
    lines: list[str] = []

    lines.append(f"**{msg.title}**")
    lines.append("")

    discount = _calc_discount(msg.snapshot)
    price_str = f"**{msg.snapshot.current_price:.2f} {msg.snapshot.currency}**"
    if discount:
        price_str += f" (-{discount})"
    lines.append(f"> 当前价: {price_str}")

    low_date = ""
    if msg.snapshot.historical_low_at:
        low_date = f" ({msg.snapshot.historical_low_at.strftime('%Y-%m-%d')})"
    lines.append(f"> 史低价: {msg.snapshot.historical_low:.2f} {msg.snapshot.currency}{low_date}")

    if msg.profile:
        if msg.snapshot.store:
            lines.append(f"> 商店: {msg.snapshot.store}")
        if msg.profile.developer:
            lines.append(f"> 开发商: {msg.profile.developer}")
        if msg.profile.publisher and msg.profile.publisher != msg.profile.developer:
            lines.append(f"> 发行商: {msg.profile.publisher}")
        review = _format_review_score(msg.profile)
        if review:
            lines.append(f"> 评测: {review}")
        tags = _format_tags(msg.profile)
        if tags:
            lines.append(f"> 标签: {tags}")
        platforms = _format_platforms(msg.profile)
        if platforms:
            lines.append(f"> 平台: {platforms}")
    elif msg.snapshot.store:
        lines.append(f"> 商店: {msg.snapshot.store}")

    lines.append(f"> 口径: {msg.window} ・ 来源: {msg.snapshot.source}")

    if msg.steam_url:
        lines.append("")
        lines.append(f"[Steam Store]({msg.steam_url})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_plain_text(snapshot: PriceSnapshot, tag: str, window: str) -> str:
    lines: list[str] = [f"【{tag}】{snapshot.title}"]
    lines.append(f"当前价: {snapshot.current_price:.2f} {snapshot.currency}")
    lines.append(f"史低价: {snapshot.historical_low:.2f} {snapshot.currency}")
    if snapshot.store:
        lines.append(f"商店: {snapshot.store}")
    lines.append(f"来源: {snapshot.source}")
    lines.append(f"口径: {window}")
    return "\n".join(lines)


def _calc_discount(snapshot: PriceSnapshot) -> str | None:
    if snapshot.historical_low > 0 and snapshot.current_price < snapshot.historical_low:
        pct = (1 - snapshot.current_price / snapshot.historical_low) * 100
        if pct >= 1:
            return f"{pct:.0f}%"
    return None


def _format_review_score(profile: GameProfile) -> str | None:
    total = profile.positive_count + profile.negative_count
    if total == 0:
        return None
    pct = profile.positive_count / total * 100
    if total >= 1000:
        count_str = f"{total // 1000},{total % 1000:03d}"
    else:
        count_str = str(total)
    return f"{pct:.0f}% ({count_str} reviews)"


def _format_platforms(profile: GameProfile) -> str | None:
    if not profile.platforms:
        return None
    parts: list[str] = []
    if profile.platforms.get("windows"):
        parts.append("Win")
    if profile.platforms.get("mac"):
        parts.append("Mac")
    if profile.platforms.get("linux"):
        parts.append("Linux")
    return "/".join(parts) if parts else None


def _format_tags(profile: GameProfile, limit: int = 5) -> str | None:
    if not profile.tags:
        return None
    return ", ".join(profile.tags[:limit])


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_TG_MD_SPECIALS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_telegram_md(text: str) -> str:
    return _TG_MD_SPECIALS.sub(r"\\\1", text)


def _esc_inline(text: str) -> str:
    return _escape_telegram_md(text)


def _esc_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
