from __future__ import annotations

import asyncio
import difflib
import html
import re
from dataclasses import dataclass, field

from valveye.config import settings
from valveye.domain import GameProfile, RecommendationItem, RecommendationReason
from valveye.game_data import (
    GameDataService,
    _merge_tags,
    _extract_display_tags,
    _extract_relevance_tags,
    _extract_tags_from_steamspy,
    is_feature_tag as _is_feature_tag,
)

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SPACE_RE = re.compile(r"\s+")
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
    lowered = re.sub(r"[^a-z0-9一-鿿]+", " ", lowered)
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


def _backfill_search_key(title: str) -> str:
    normalized = html.unescape(title or "").lower()
    for token in sorted(_VARIANT_KEYWORDS, key=len, reverse=True):
        normalized = normalized.replace(token, " ")
    for token in sorted(_NOISE_KEYWORDS, key=len, reverse=True):
        normalized = normalized.replace(token, " ")
    normalized = normalized.replace("digital", " ")
    normalized = _YEAR_RE.sub(" ", normalized)
    normalized = re.sub(r"[^a-z0-9一-鿿]+", " ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    return normalized


@dataclass(slots=True)
class _Candidate:
    profile: GameProfile
    signals: set[str] = field(default_factory=set)


class Recommender:
    def __init__(self, data_service: GameDataService | None = None):
        self._data = data_service or GameDataService()
        self._candidate_pool = max(20, settings.steam_recommend_candidate_pool)
        self._negative_review_count = max(1, settings.steam_recommend_negative_review_count)

    async def recommend(self, game_query: str, top_n: int = 10) -> list[dict]:
        if not (game_query or "").strip():
            return []

        en_name, resolved_app_id = await self._data.resolve_game(game_query)

        target = await self._resolve_target_profile(
            game_query=en_name, resolved_app_id=resolved_app_id,
        )
        if target is None:
            return await self._fallback_name_similarity(game_query=en_name, top_n=top_n)

        candidates = await self._collect_candidates(target=target, game_query=en_name)
        if not candidates:
            return []

        ranked = await self._rank_with_reasons(
            target=target,
            candidates=candidates,
            game_query=en_name,
            top_n=top_n,
        )
        return [row.to_dict() for row in ranked]

    async def search_candidates(self, game_query: str, top_n: int = 15) -> list[dict]:
        """Public method for LLM tools: returns lightweight candidate list without full ranking."""
        if not (game_query or "").strip():
            return []

        en_name, resolved_app_id = await self._data.resolve_game(game_query)
        target = await self._resolve_target_profile(
            game_query=en_name, resolved_app_id=resolved_app_id,
        )
        if target is None:
            return []

        candidates = await self._collect_candidates(target=target, game_query=en_name)
        if not candidates:
            return []

        # Lightweight sort: prioritize multi-signal candidates, then by tag overlap
        target_tag_set = {t.lower() for t in target.relevance_tags if t}
        scored: list[tuple[float, _Candidate]] = []
        for cand in candidates.values():
            candidate_tags = {t.lower() for t in cand.profile.relevance_tags if t}
            inter = target_tag_set.intersection(candidate_tags)
            union = target_tag_set.union(candidate_tags)
            tag_score = (len(inter) / len(union)) if union else 0.0
            signal_bonus = 0.15 if len(cand.signals) > 1 else 0.0
            scored.append((tag_score + signal_bonus, cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, cand in scored[:top_n]:
            results.append({
                "title": cand.profile.title,
                "app_id": cand.profile.app_id,
                "tags": cand.profile.tags[:8],
                "negative_ratio": round(cand.profile.negative_ratio, 4) if cand.profile.negative_ratio is not None else None,
                "source_signals": sorted(cand.signals),
                "thumb": cand.profile.thumb,
            })
        return results

    async def _resolve_target_profile(
        self,
        game_query: str,
        resolved_app_id: int | None = None,
    ) -> GameProfile | None:
        if resolved_app_id:
            profile = await self._data.fetch_profile(app_id=resolved_app_id)
            if profile:
                return profile

        search_rows = await self._data.search(term=game_query, limit=8)
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

        return await self._data.fetch_profile(app_id=app_id)

    async def _collect_candidates(
        self,
        target: GameProfile,
        game_query: str,
    ) -> dict[int, _Candidate]:
        candidates: dict[int, _Candidate] = {}
        target_tags = target.relevance_tags[:5]

        tag_terms = target_tags if target_tags else [game_query]
        for term in tag_terms:
            rows = await self._data.search(term=term, limit=15)
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
                candidates.setdefault(
                    app_id_i,
                    _Candidate(profile=GameProfile(app_id=app_id_i, title=title, app_type="")),
                ).signals.add("tag_search")

        similar_ids = await self._data.fetch_more_like_this_ids(app_id=target.app_id)
        for sid in similar_ids:
            if sid == target.app_id:
                continue
            candidates.setdefault(
                sid,
                _Candidate(profile=GameProfile(app_id=sid, title="", app_type="")),
            ).signals.add("more_like_this")

        if not candidates:
            return {}

        limited_ids = list(candidates.keys())[: self._candidate_pool]
        sem = asyncio.Semaphore(6)

        async def _load(app_id: int) -> tuple[int, GameProfile | None]:
            async with sem:
                profile = await self._data.fetch_profile(app_id=app_id)
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
        target: GameProfile,
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

            downside = await self._build_downside(profile=cand.profile)
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

    async def _build_downside(self, profile: GameProfile) -> str:
        samples = await self._data.fetch_reviews(
            app_id=profile.app_id, review_type="negative", count=self._negative_review_count,
        )
        if samples:
            return f"差评关注：{'；'.join(samples[: self._negative_review_count])}"

        if profile.negative_ratio is not None:
            ratio_pct = int(profile.negative_ratio * 100)
            if ratio_pct >= 45:
                return f"差评占比约 {ratio_pct}%（样本聚合统计）"
            if ratio_pct >= 30:
                return f"差评占比约 {ratio_pct}%，建议看近期评测"

        return "暂无足够差评样本"

    async def _fallback_name_similarity(self, game_query: str, top_n: int) -> list[dict]:
        session = await self._data._get_session()
        try:
            async with session.get(
                f"{settings.cheapshark_base_url}/games",
                params={"title": game_query, "limit": 20, "exact": 0},
            ) as resp:
                if resp.status >= 400:
                    return []
                games = await resp.json()
        except Exception:
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
        backfill_cache: dict[str, GameProfile | None] = {}

        for item in selected:
            if item.app_id > 0:
                continue
            backfill_key = _backfill_search_key(item.title)
            if not backfill_key:
                continue
            if backfill_key not in backfill_cache:
                rows = await self._data.search(term=backfill_key, limit=1)
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

                profile = await self._data.fetch_profile(app_id=app_id)
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

    # Keep static methods accessible for tests
    _extract_display_tags_from_details = staticmethod(_extract_display_tags)
    _extract_relevance_tags_from_details = staticmethod(_extract_relevance_tags)
    _extract_tags_from_steamspy = staticmethod(_extract_tags_from_steamspy)
    _merge_tags = staticmethod(_merge_tags)
