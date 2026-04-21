from valveye.subscriptions import SubscriptionRepository


def test_add_skips_duplicate_active_subscription(tmp_path):
    repo = SubscriptionRepository(db_path=str(tmp_path / "subscriptions.sqlite3"))

    channels_a = [{"type": "email", "to": "user@example.com"}]
    channels_b = [{"to": "user@example.com", "type": "email"}]

    first_id, created_first = repo.add(
        user_id="u1",
        game_query="Hades",
        window="all",
        region="CN",
        currency="CNY",
        channels=channels_a,
    )
    second_id, created_second = repo.add(
        user_id="u1",
        game_query="Hades",
        window="all",
        region="CN",
        currency="CNY",
        channels=channels_b,
    )

    assert created_first is True
    assert created_second is False
    assert first_id == second_id
    assert [sub.id for sub in repo.list_active()] == [first_id]


def test_add_allows_different_channels(tmp_path):
    repo = SubscriptionRepository(db_path=str(tmp_path / "subscriptions.sqlite3"))

    repo.add(
        user_id="u1",
        game_query="Hades",
        window="all",
        region="CN",
        currency="CNY",
        channels=[{"type": "email", "to": "user@example.com"}],
    )
    second_id, created_second = repo.add(
        user_id="u1",
        game_query="Hades",
        window="all",
        region="CN",
        currency="CNY",
        channels=[{"type": "telegram", "chat_id": "123"}],
    )

    assert created_second is True
    assert second_id == 2
    assert [sub.id for sub in repo.list_active()] == [2, 1]