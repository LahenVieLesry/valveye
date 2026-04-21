from types import SimpleNamespace

import pytest

from trigger_low_price_notifications import SelectionSpec, _build_selection_spec, parse_id_list, select_subscriptions
from valveye.domain import Subscription


def _sub(sub_id: int) -> Subscription:
    return Subscription(
        id=sub_id,
        user_id=f"u{sub_id}",
        game_query=f"Game{sub_id}",
        window="all",
        region="CN",
        currency="CNY",
        channels=[],
        active=True,
        last_notified_low=None,
        last_notified_at=None,
    )


def test_parse_id_list_supports_commas_and_spaces():
    assert parse_id_list("1, 3,4") == [1, 3, 4]


def test_build_selection_spec_defaults_to_all():
    args = SimpleNamespace(top_n=None, ids=None, all=False)
    spec = _build_selection_spec(args)
    assert spec == SelectionSpec(top_n=None, ids=None, all_active=True)


def test_select_subscriptions_by_ids_keeps_requested_order():
    subs = [_sub(1), _sub(2), _sub(3), _sub(4)]
    spec = SelectionSpec(ids=[4, 1])
    selected = select_subscriptions(subs, spec)
    assert [sub.id for sub in selected] == [4, 1]


def test_select_subscriptions_by_top_n():
    subs = [_sub(1), _sub(2), _sub(3), _sub(4)]
    spec = SelectionSpec(top_n=2)
    selected = select_subscriptions(subs, spec)
    assert [sub.id for sub in selected] == [1, 2]


def test_build_selection_spec_rejects_conflicting_arguments():
    args = SimpleNamespace(top_n=2, ids="1,2", all=False)
    with pytest.raises(ValueError):
        _build_selection_spec(args)