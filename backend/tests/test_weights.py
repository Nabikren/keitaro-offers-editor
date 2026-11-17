import itertools
import random

import pytest

from app.domain.weights import Slot, WeightError, distribute


def shares(*slots):
    return list(distribute(list(slots)).values())


def test_empty_stream_has_no_shares():
    assert distribute([]) == {}


@pytest.mark.parametrize(
    "count, expected",
    [(1, [100]), (2, [50, 50]), (3, [34, 33, 33]), (4, [25, 25, 25, 25]), (6, [17, 17, 17, 17, 16, 16]), (7, [15, 15, 14, 14, 14, 14, 14])],
)
def test_equal_split_gives_remainder_to_first_in_order(count, expected):
    assert shares(*(Slot(i) for i in range(count))) == expected


def test_pin_keeps_value_and_splits_rest_like_video():
    # Видео 5:32: закреплено 25%, двое свободных получают 38/37.
    assert distribute([Slot(1), Slot(2), Slot(3, pinned=True, share=25)]) == {1: 38, 2: 37, 3: 25}


def test_pinned_values_never_change_implicitly():
    result = distribute([Slot(1, True, 10), Slot(2), Slot(3, True, 45), Slot(4)])
    assert result[1] == 10 and result[3] == 45
    assert result[2] + result[4] == 45


def test_pinned_sum_over_100_is_rejected():
    with pytest.raises(WeightError) as e:
        distribute([Slot(1, True, 60), Slot(2, True, 50)])
    assert e.value.code == "pinned_over_100"


def test_all_pinned_must_sum_to_100():
    assert distribute([Slot(1, True, 70), Slot(2, True, 30)]) == {1: 70, 2: 30}
    with pytest.raises(WeightError) as e:
        distribute([Slot(1, True, 70), Slot(2, True, 20)])
    assert e.value.code == "pinned_not_100"


def test_pin_100_with_other_active_offers_is_rejected():
    with pytest.raises(WeightError) as e:
        distribute([Slot(1, True, 100), Slot(2)])
    assert e.value.code == "no_free_share"


def test_pin_100_alone_is_allowed():
    assert distribute([Slot(1, True, 100)]) == {1: 100}


def test_each_free_offer_needs_at_least_one_percent():
    with pytest.raises(WeightError):
        distribute([Slot(1, True, 99), Slot(2), Slot(3)])
    assert distribute([Slot(1, True, 98), Slot(2), Slot(3)]) == {1: 98, 2: 1, 3: 1}


@pytest.mark.parametrize("value", [0, -5, 101])
def test_pin_value_out_of_range(value):
    with pytest.raises(WeightError) as e:
        distribute([Slot(1, True, value), Slot(2)])
    assert e.value.code == "pin_out_of_range"


def test_duplicate_offer_rejected():
    with pytest.raises(WeightError):
        distribute([Slot(1), Slot(1)])


def test_property_sum_is_100_and_deterministic():
    rnd = random.Random(42)
    for _ in range(500):
        n = rnd.randint(1, 12)
        slots = []
        budget = 100 - n
        for i in range(n):
            if rnd.random() < 0.3 and budget > 1:
                v = rnd.randint(1, max(1, budget // 2))
                budget -= v
                slots.append(Slot(i, True, v))
            else:
                slots.append(Slot(i))
        if all(s.pinned for s in slots):
            continue
        first = distribute(slots)
        assert first == distribute(slots)
        assert sum(first.values()) == 100
        assert all(v >= 1 for v in first.values())
        free = [first[s.key] for s in slots if not s.pinned]
        assert max(free) - min(free) <= 1
        assert free == sorted(free, reverse=True)  # остаток уходит первым по порядку


def test_order_matters_only_for_remainder():
    for perm in itertools.permutations([1, 2, 3]):
        result = distribute([Slot(k) for k in perm])
        assert result[perm[0]] == 34
