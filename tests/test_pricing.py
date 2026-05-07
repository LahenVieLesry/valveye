from datetime import UTC, datetime, timedelta

from valveye.domain import PricePoint, PriceSnapshot
from valveye.pricing import PriceService


def test_low_decision_at_low_and_new_low():
    snapshot = PriceSnapshot(
        source="mock",
        game_id="1",
        title="Test",
        currency="USD",
        current_price=9.99,
        historical_low=9.99,
        historical_low_at=datetime.now(tz=UTC),
    )
    decision = PriceService.evaluate_low(snapshot=snapshot, window="all", known_notified_low=10.99)
    assert decision.is_at_low is True
    assert decision.is_new_low is True
    assert decision.window_low == 9.99


def test_window_low_filters_history():
    """window_low should be the min price within the window, not the all-time low."""
    now = datetime.now(tz=UTC)
    snapshot = PriceSnapshot(
        source="mock",
        game_id="1",
        title="Test",
        currency="USD",
        current_price=12.99,
        historical_low=5.0,  # all-time low is 5.0
        historical_low_at=now - timedelta(days=400),
        history=[
            PricePoint(price=15.0, timestamp=now - timedelta(days=400)),
            PricePoint(price=5.0, timestamp=now - timedelta(days=400)),  # old low, outside 3m
            PricePoint(price=12.99, timestamp=now - timedelta(days=10)),
        ],
    )
    decision = PriceService.evaluate_low(snapshot=snapshot, window="3m", known_notified_low=None)
    # 3m window low should be 12.99, not 5.0
    assert decision.window_low == 12.99
    assert decision.is_at_low is True
    assert decision.is_new_low is True  # first notification (known_notified_low=None)


def test_window_low_roundtrip():
    """Passing window_low back as known_notified_low should yield is_new_low=False."""
    now = datetime.now(tz=UTC)
    snapshot = PriceSnapshot(
        source="mock",
        game_id="1",
        title="Test",
        currency="USD",
        current_price=12.99,
        historical_low=5.0,
        historical_low_at=now - timedelta(days=400),
        history=[
            PricePoint(price=15.0, timestamp=now - timedelta(days=400)),
            PricePoint(price=5.0, timestamp=now - timedelta(days=400)),
            PricePoint(price=12.99, timestamp=now - timedelta(days=10)),
        ],
    )
    decision = PriceService.evaluate_low(snapshot=snapshot, window="3m", known_notified_low=None)
    # Simulate marking notified with window_low
    decision2 = PriceService.evaluate_low(snapshot=snapshot, window="3m", known_notified_low=decision.window_low)
    assert decision2.is_new_low is False
