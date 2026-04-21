from __future__ import annotations

import asyncio
import difflib
import html
import re
import ssl
from dataclasses import dataclass, field

import aiohttp
import certifi

from valveye.config import settings
from valveye.domain import RecommendationItem, RecommendationReason


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_MORE_LIKE_APP_RE = re.compile(r"/app/(\d+)")
_SPACE_RE = re.compile(r"\s+")
_FEATURE_TAGS = {
    "single-player",
    "single player",
    "multiplayer",
    "multi-player",
    "co-op",
    "co op",
    "online co-op",
    "online co op",
    "shared/split screen co-op",
    "shared/split screen co op",
    "shared/split screen",
    "steam achievements",
    "full controller support",
    "steam cloud",
    "hdr available",
    "family sharing",
    "remote play on phone",
    "remote play on tablet",
    "remote play on tv",
    "remote play together",
    "commentary available",
    "steam trading cards",
    "steam workshop",
    "stats",
    "save anytime",
    "includes level editor",
    "captions available",
    "playable without timed input",
    "camera comfort",
    "custom volume controls",
    "stereo sound",
    "surround sound",
}
_NOISE_KEYWORDS = {
    "dlc",
    "soundtrack",
    "bundle",
    "pack",
    "season pass",
    "expansion",
    "demo",
    "ost",
    "test server",
    "beta",
    "pts",
    "add-on",
}
_VARIANT_KEYWORDS = {
    "edition",
    "definitive",
    "deluxe",
    "ultimate",
    "complete",
    "goty",
    "anniversary",
    "remaster",
    "remastered",
    "director's cut",
    "director cut",
}


def _extract_year(title: str) -> int | None:
    m = _YEAR_RE.search(title)
    if not m:
        return None
    try:
        return int(m.group(0))
    except (TypeError, ValueError):
        return None


def _normalize_title(title: str) -> str:
    lowered = html.unescape(title or "").lower()
    lowered = _YEAR_RE.sub(" ", lowered)
    lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", lowered)
    lowered = _SPACE_RE.sub(" ", lowered).strip()
    return lowered


def _is_noise_title(title: str) -> bool:
    lowered = (title or "").lower()
    if any(token in lowered for token in _NOISE_KEYWORDS):
        return True
    return False


def _is_variant_title(title: str, target_title: str) -> bool:
    lowered = (title or "").lower()
    if any(token in lowered for token in _VARIANT_KEYWORDS):
        return True

    target_norm = _normalize_title(target_title)
    candidate_norm = _normalize_title(title)
    if not target_norm or not candidate_norm:
        return False
    if target_norm != candidate_norm:
        return False

    target_year = _extract_year(target_title)
    candidate_year = _extract_year(title)
    return target_year != candidate_year and candidate_year is not None


def _clip(text: str, limit: int = 110) -> str:
    cleaned = _SPACE_RE.sub(" ", text.replace("\n", " ")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}…"


def _is_feature_tag(tag: str) -> bool:
    normalized = (tag or "").strip().lower()
    return normalized in _FEATURE_TAGS or normalized.startswith("remote play")


def _backfill_search_key(title: str) -> str:
    normalized = html.unescape(title or "").lower()
    for token in sorted(_VARIANT_KEYWORDS, key=len, reverse=True):
        normalized = normalized.replace(token, " ")
    for token in sorted(_NOISE_KEYWORDS, key=len, reverse=True):
        normalized = normalized.replace(token, " ")
    normalized = normalized.replace("digital", " ")
    normalized = _YEAR_RE.sub(" ", normalized)
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    return normalized


@dataclass(slots=True)
class _AppProfile:
    app_id: int
    title: str
    app_type: str
    tags: list[str] = field(default_factory=list)
    relevance_tags: list[str] = field(default_factory=list)
    description: str = ""
    thumb: str | None = None
    negative_ratio: float | None = None


@dataclass(slots=True)
class _Candidate:
    profile: _AppProfile
    signals: set[str] = field(default_factory=set)


class Recommender:
    def __init__(self):
        self._timeout_sec = max(8, settings.steam_recommend_timeout_sec)
        self._candidate_pool = max(20, settings.steam_recommend_candidate_pool)
        self._negative_review_count = max(1, settings.steam_recommend_negative_review_count)

    async def recommend(self, game_query: str, top_n: int = 10) -> list[dict]:
        if not (game_query or "").strip():
            return []

        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            target = await self._resolve_target_profile(session=session, game_query=game_query)
            if target is None:
                return await self._fallback_name_similarity(session=session, game_query=game_query, top_n=top_n)

            candidates = await self._collect_candidates(session=session, target=target, game_query=game_query)
            if not candidates:
                return []

            ranked = await self._rank_with_reasons(
                session=session,
                target=target,
                candidates=candidates,
                game_query=game_query,
                top_n=top_n,
            )
            return [row.to_dict() for row in ranked]

    async def _resolve_target_profile(self, session: aiohttp.ClientSession, game_query: str) -> _AppProfile | None:
        search_rows = await self._store_search(session=session, term=game_query, limit=8)
        if not search_rows:
            return None

        best_row: dict | None = None
        best_score = -1.0
        for row in search_rows:
            title = str(row.get("name") or "")
            if not title or _is_noise_title(title):
                continue
            score = difflib.SequenceMatcher(a=game_query.lower(), b=title.lower()).ratio()
            if score > best_score:
                best_score = score
                best_row = row

        if not best_row:
            return None

        app_id_raw = best_row.get("id")
        try:
            app_id = int(app_id_raw)
        except (TypeError, ValueError):
            return None

        return await self._fetch_profile(session=session, app_id=app_id)

    async def _collect_candidates(
        self,
        session: aiohttp.ClientSession,
        target: _AppProfile,
        game_query: str,
    ) -> dict[int, _Candidate]:
        candidates: dict[int, _Candidate] = {}
        target_tags = target.relevance_tags[:5]

        tag_terms = target_tags if target_tags else [game_query]
        for term in tag_terms:
            rows = await self._store_search(session=session, term=term, limit=15)
            for row in rows:
                app_id = row.get("id")
                try:
                    app_id_i = int(app_id)
                except (TypeError, ValueError):
                    continue
                if app_id_i == target.app_id:
                    continue
                title = str(row.get("name") or "")
                if not title:
                    continue
                if _is_noise_title(title) or _is_variant_title(title=title, target_title=target.title):
                    continue
                candidates.setdefault(app_id_i, _Candidate(profile=_AppProfile(app_id=app_id_i, title=title, app_type=""))).signals.add(
                    "tag_search"
                )

        similar_ids = await self._fetch_more_like_this_ids(session=session, app_id=target.app_id)
        for sid in similar_ids:
            if sid == target.app_id:
                continue
            candidates.setdefault(sid, _Candidate(profile=_AppProfile(app_id=sid, title="", app_type=""))).signals.add("more_like_this")

        if not candidates:
            return {}

        limited_ids = list(candidates.keys())[: self._candidate_pool]
        sem = asyncio.Semaphore(6)

        async def _load(app_id: int) -> tuple[int, _AppProfile | None]:
            async with sem:
                profile = await self._fetch_profile(session=session, app_id=app_id)
                return app_id, profile

        loaded = await asyncio.gather(*[_load(app_id) for app_id in limited_ids], return_exceptions=False)
        enriched: dict[int, _Candidate] = {}
        for app_id, profile in loaded:
            if profile is None:
                continue
            if profile.app_type != "game":
                continue
            if _is_noise_title(profile.title):
                continue
            if _is_variant_title(title=profile.title, target_title=target.title):
                continue
            if _normalize_title(profile.title) == _normalize_title(target.title):
                continue
            c = candidates.get(app_id)
            if c is None:
                continue
            c.profile = profile
            enriched[app_id] = c

        return enriched

    async def _rank_with_reasons(
        self,
        session: aiohttp.ClientSession,
        target: _AppProfile,
        candidates: dict[int, _Candidate],
        game_query: str,
        top_n: int,
    ) -> list[RecommendationItem]:
        target_tag_set = {t.lower() for t in target.relevance_tags if t}
        rows: list[RecommendationItem] = []
        for cand in candidates.values():
            title = cand.profile.title
            candidate_tags = {t.lower() for t in cand.profile.relevance_tags if t}

            inter = sorted(target_tag_set.intersection(candidate_tags))
            union = target_tag_set.union(candidate_tags)
            tag_score = (len(inter) / len(union)) if union else 0.0

            similar_score = 1.0 if "more_like_this" in cand.signals else 0.0
            title_score = difflib.SequenceMatcher(a=game_query.lower(), b=title.lower()).ratio()
            final_score = 0.7 * tag_score + 0.2 * similar_score + 0.1 * title_score

            downside = await self._build_downside(session=session, profile=cand.profile)
            reason = RecommendationReason(
                tag_overlap=inter[:4],
                matched_signals=sorted(cand.signals),
                downside=downside,
            )
            rows.append(
                RecommendationItem(
                    title=title,
                    app_id=cand.profile.app_id,
                    score=final_score,
                    tags=cand.profile.tags[:8],
                    similarity_breakdown={
                        "tag_similarity": tag_score,
                        "more_like_this": similar_score,
                        "title_similarity": title_score,
                    },
                    reason=reason,
                    source_signals=sorted(cand.signals),
                    thumb=cand.profile.thumb,
                )
            )

        rows.sort(key=lambda x: x.score, reverse=True)
        return rows[:top_n]

    async def _build_downside(self, session: aiohttp.ClientSession, profile: _AppProfile) -> str:
        samples = await self._fetch_negative_review_samples(session=session, app_id=profile.app_id)
        if samples:
            return f"差评关注：{'；'.join(samples[: self._negative_review_count])}"

        if profile.negative_ratio is not None:
            ratio_pct = int(profile.negative_ratio * 100)
            if ratio_pct >= 45:
                return f"差评占比约 {ratio_pct}%（样本聚合统计）"
            if ratio_pct >= 30:
                return f"差评占比约 {ratio_pct}%，建议看近期评测"

        return "暂无足够差评样本"

    async def _fetch_profile(self, session: aiohttp.ClientSession, app_id: int) -> _AppProfile | None:
        details = await self._fetch_store_appdetails(session=session, app_id=app_id)
        if not details:
            return None

        tags = self._extract_display_tags_from_details(details)
        spy = await self._fetch_steamspy_details(session=session, app_id=app_id)
        if spy:
            tags = self._merge_tags(tags, self._extract_tags_from_steamspy(spy))

        negative_ratio = None
        if spy:
            positive = spy.get("positive")
            negative = spy.get("negative")
            try:
                pos_i = int(positive)
                neg_i = int(negative)
                total = pos_i + neg_i
                if total > 0:
                    negative_ratio = neg_i / total
            except (TypeError, ValueError):
                negative_ratio = None

        return _AppProfile(
            app_id=app_id,
            title=str(details.get("name") or ""),
            app_type=str(details.get("type") or ""),
            tags=self._extract_display_tags_from_details(details, tags),
            relevance_tags=self._extract_relevance_tags_from_details(details),
            description=str(details.get("short_description") or ""),
            thumb=str(details.get("header_image") or "") or None,
            negative_ratio=negative_ratio,
        )

    async def _store_search(self, session: aiohttp.ClientSession, term: str, limit: int = 10) -> list[dict]:
        if not term.strip():
            return []
        url = f"{settings.steam_store_base_url.rstrip('/')}/api/storesearch"
        try:
            async with session.get(
                url,
                params={
                    "term": term,
                    "l": "english",
                    "cc": "us",
                },
            ) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
        except ValueError:
            return []

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [it for it in items[:limit] if isinstance(it, dict)]

    async def _fetch_store_appdetails(self, session: aiohttp.ClientSession, app_id: int) -> dict | None:
        url = f"{settings.steam_store_base_url.rstrip('/')}/api/appdetails"
        try:
            async with session.get(
                url,
                params={"appids": app_id, "l": "english", "cc": "us"},
            ) as resp:
                if resp.status >= 400:
                    return None
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        root = payload.get(str(app_id)) if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return None
        if root.get("success") is not True:
            return None
        data = root.get("data")
        return data if isinstance(data, dict) else None

    async def _fetch_steamspy_details(self, session: aiohttp.ClientSession, app_id: int) -> dict | None:
        url = f"{settings.steamspy_api_base_url.rstrip('/')}/api.php"
        try:
            async with session.get(url, params={"request": "appdetails", "appid": app_id}) as resp:
                if resp.status >= 400:
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _fetch_more_like_this_ids(self, session: aiohttp.ClientSession, app_id: int) -> list[int]:
        url = f"{settings.steam_store_base_url.rstrip('/')}/recommended/morelike/app/{app_id}"
        try:
            async with session.get(url, params={"l": "english"}) as resp:
                if resp.status >= 400:
                    return []
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

        found = _MORE_LIKE_APP_RE.findall(body)
        uniq: dict[int, None] = {}
        for raw in found:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if sid != app_id:
                uniq[sid] = None
        return list(uniq.keys())

    async def _fetch_negative_review_samples(self, session: aiohttp.ClientSession, app_id: int) -> list[str]:
        url = f"{settings.steam_store_base_url.rstrip('/')}/appreviews/{app_id}"
        try:
            async with session.get(
                url,
                params={
                    "json": 1,
                    "language": "all",
                    "filter": "recent",
                    "review_type": "negative",
                    "purchase_type": "all",
                    "num_per_page": self._negative_review_count,
                },
            ) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

        rows = payload.get("reviews") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []

        snippets: list[str] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            text = r.get("review")
            if not isinstance(text, str) or not text.strip():
                continue
            snippets.append(_clip(text))
            if len(snippets) >= self._negative_review_count:
                break
        return snippets

    @staticmethod
    def _extract_display_tags_from_details(details: dict, base_tags: list[str] | None = None) -> list[str]:
        tags: list[str] = []
        genres = details.get("genres")
        if isinstance(genres, list):
            for item in genres:
                if isinstance(item, dict):
                    desc = item.get("description")
                    if isinstance(desc, str) and desc.strip():
                        tags.append(desc.strip())

        categories = details.get("categories")
        if isinstance(categories, list):
            for item in categories:
                if isinstance(item, dict):
                    desc = item.get("description")
                    if isinstance(desc, str) and desc.strip():
                        tags.append(desc.strip())

        if base_tags:
            tags = [*base_tags, *tags]

        return Recommender._merge_tags([], tags)

    @staticmethod
    def _extract_relevance_tags_from_details(details: dict) -> list[str]:
        genres: list[str] = []
        genre_rows = details.get("genres")
        if isinstance(genre_rows, list):
            for item in genre_rows:
                if not isinstance(item, dict):
                    continue
                desc = item.get("description")
                if not isinstance(desc, str):
                    continue
                cleaned = desc.strip()
                if cleaned and not _is_feature_tag(cleaned):
                    genres.append(cleaned)

        if genres:
            return Recommender._merge_tags([], genres)

        fallback: list[str] = []
        category_rows = details.get("categories")
        if isinstance(category_rows, list):
            for item in category_rows:
                if not isinstance(item, dict):
                    continue
                desc = item.get("description")
                if not isinstance(desc, str):
                    continue
                cleaned = desc.strip()
                if cleaned and not _is_feature_tag(cleaned):
                    fallback.append(cleaned)

        return Recommender._merge_tags([], fallback)

    @staticmethod
    def _extract_tags_from_steamspy(payload: dict) -> list[str]:
        tag_map = payload.get("tags")
        if not isinstance(tag_map, dict):
            return []

        weighted: list[tuple[int, str]] = []
        for key, votes in tag_map.items():
            if not isinstance(key, str) or not key.strip():
                continue
            try:
                weight = int(votes)
            except (TypeError, ValueError):
                weight = 0
            weighted.append((weight, key.strip()))

        weighted.sort(key=lambda x: x[0], reverse=True)
        return [name for _, name in weighted[:10]]

    @staticmethod
    def _merge_tags(base: list[str], extra: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for raw in [*base, *extra]:
            if not isinstance(raw, str):
                continue
            cleaned = raw.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key not in seen:
                seen[key] = cleaned
        return list(seen.values())

    async def _fallback_name_similarity(self, session: aiohttp.ClientSession, game_query: str, top_n: int) -> list[dict]:
        try:
            async with session.get(
                f"{settings.cheapshark_base_url}/games",
                params={"title": game_query, "limit": 20, "exact": 0},
            ) as resp:
                if resp.status >= 400:
                    return []
                games = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

        scored: list[RecommendationItem] = []
        for g in games if isinstance(games, list) else []:
            if not isinstance(g, dict):
                continue
            title = str(g.get("external") or "")
            if not title:
                continue
            if title.lower() == game_query.lower():
                continue
            if _is_variant_title(title=title, target_title=game_query):
                continue
            if _is_noise_title(title):
                continue
            score = difflib.SequenceMatcher(a=game_query.lower(), b=title.lower()).ratio()
            raw_app_id = g.get("steamAppID")
            try:
                app_id = int(raw_app_id)
            except (TypeError, ValueError):
                app_id = 0
            scored.append(
                RecommendationItem(
                    title=title,
                    app_id=app_id,
                    score=score,
                    tags=[],
                    similarity_breakdown={"title_similarity": score},
                    reason=RecommendationReason(
                        tag_overlap=[],
                        matched_signals=["cheapshark_fallback"],
                        downside="仅名称相似度兜底，建议二次确认",
                    ),
                    source_signals=["cheapshark_fallback"],
                    thumb=(str(g.get("thumb")) if g.get("thumb") else None),
                )
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        selected = scored[:top_n]
        backfill_cache: dict[str, _AppProfile | None] = {}

        for item in selected:
            if item.app_id > 0:
                continue
            backfill_key = _backfill_search_key(item.title)
            if not backfill_key:
                continue
            if backfill_key not in backfill_cache:
                rows = await self._store_search(session=session, term=backfill_key, limit=1)
                if not rows:
                    backfill_cache[backfill_key] = None
                    continue
                first = rows[0]
                app_id_raw = first.get("id") if isinstance(first, dict) else None
                try:
                    app_id = int(app_id_raw)
                except (TypeError, ValueError):
                    backfill_cache[backfill_key] = None
                    continue

                profile = await self._fetch_profile(session=session, app_id=app_id)
                if profile is None:
                    backfill_cache[backfill_key] = None
                    continue
                if _is_noise_title(profile.title) or _is_variant_title(title=profile.title, target_title=game_query):
                    backfill_cache[backfill_key] = None
                    continue
                backfill_cache[backfill_key] = profile

            profile = backfill_cache.get(backfill_key)
            if profile is None:
                continue

            item.app_id = profile.app_id
            if profile.tags:
                item.tags = profile.tags[:8]

        return [item.to_dict() for item in selected]
