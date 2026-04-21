from valveye.recommendation import (
    _backfill_search_key,
    _is_noise_title,
    _is_feature_tag,
    _is_variant_title,
    Recommender,
)


def test_noise_title_filters_dlc_bundle_soundtrack():
    assert _is_noise_title("Portal 2 DLC") is True
    assert _is_noise_title("A and B Bundle") is True
    assert _is_noise_title("Game OST Soundtrack") is True
    assert _is_noise_title("Hollow Knight") is False


def test_variant_title_filters_edition_and_year_variant():
    assert _is_variant_title("Game Deluxe Edition", "Game") is True
    assert _is_variant_title("FIFA 2026", "FIFA 2025") is True
    assert _is_variant_title("Hades II", "Hades") is False


def test_variant_title_non_variant_regular_title():
    assert _is_variant_title("Deep Rock Galactic", "Helldivers 2") is False


def test_variant_title_filters_deluxe_names_for_same_game_query():
    assert _is_variant_title("PRAGMATA Digital Deluxe", "PRAGMATA") is True
    assert _is_variant_title("PRAGMATA Deluxe Edition", "PRAGMATA") is True


def test_feature_tags_are_not_used_for_relevance_scoring():
    details = {
        "genres": [
            {"description": "Action"},
            {"description": "Adventure"},
        ],
        "categories": [
            {"description": "Single-player"},
            {"description": "Steam Achievements"},
            {"description": "Full controller support"},
            {"description": "Steam Cloud"},
            {"description": "Family Sharing"},
        ],
    }

    relevance = Recommender._extract_relevance_tags_from_details(details)
    display = Recommender._extract_display_tags_from_details(details)

    assert relevance == ["Action", "Adventure"]
    assert "Single-player" in display
    assert "Steam Cloud" in display
    assert _is_feature_tag("Single-player") is True
    assert _is_feature_tag("Action") is False


def test_backfill_search_key_dedupes_variant_editions():
    assert _backfill_search_key("PRAGMATA Digital Deluxe") == "pragmata"
    assert _backfill_search_key("PRAGMATA Deluxe Edition") == "pragmata"
    assert _backfill_search_key("PRAGMATA 2026") == "pragmata"
