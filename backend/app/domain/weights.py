"""Распределение целочисленных долей офферов в потоке.

Правило:
- закреплённые (pinned) доли не меняются;
- остаток 100 − сумма(pinned) делится поровну между незакреплёнными;
- целочисленный остаток деления отдаётся первым незакреплённым по порядку показа
  (3 оффера → 34/33/33; pin 25 + два свободных → 38/37, как в видео 5:32).
"""

from collections.abc import Sequence
from dataclasses import dataclass

TOTAL = 100


class WeightError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Slot:
    key: int
    pinned: bool = False
    share: int = 0  # учитывается только для закреплённых


def distribute(slots: Sequence[Slot]) -> dict[int, int]:
    """Возвращает {key: share} для активных слотов в заданном порядке."""
    if not slots:
        return {}
    keys = [s.key for s in slots]
    if len(set(keys)) != len(keys):
        raise WeightError("duplicate_offer", "Оффер встречается в потоке дважды")

    pinned = [s for s in slots if s.pinned]
    for s in pinned:
        if not 1 <= s.share <= TOTAL:
            raise WeightError("pin_out_of_range", f"Закреплённая доля должна быть от 1 до {TOTAL}%")
    pinned_sum = sum(s.share for s in pinned)
    if pinned_sum > TOTAL:
        raise WeightError("pinned_over_100", f"Сумма закреплённых долей {pinned_sum}% больше {TOTAL}%")

    free_slots = [s for s in slots if not s.pinned]
    free = TOTAL - pinned_sum
    if not free_slots:
        if pinned_sum != TOTAL:
            raise WeightError(
                "pinned_not_100",
                f"Все активные офферы закреплены, но их сумма {pinned_sum}%, а не {TOTAL}%. Открепите один из них.",
            )
        return {s.key: s.share for s in slots}
    if free < len(free_slots):
        raise WeightError(
            "no_free_share",
            f"Незакреплённым офферам остаётся {free}% на {len(free_slots)} шт.: каждому нужен хотя бы 1%",
        )

    base, rest = divmod(free, len(free_slots))
    result: dict[int, int] = {}
    free_index = 0
    for s in slots:
        if s.pinned:
            result[s.key] = s.share
        else:
            result[s.key] = base + (1 if free_index < rest else 0)
            free_index += 1
    assert sum(result.values()) == TOTAL
    return result
