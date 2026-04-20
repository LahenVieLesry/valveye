from datetime import UTC, datetime

from valveye.domain import PriceSnapshot
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
