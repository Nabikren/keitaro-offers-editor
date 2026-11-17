"""Операции черновика над связями offer↔stream.

Функции не знают о базе и HTTP: работают с любыми объектами, у которых есть поля
StreamOffer (ORM-модель или dataclass в тестах). Любая операция сначала считает
итоговые доли и только потом меняет объекты, поэтому ошибка не оставляет полуизменённый черновик.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .weights import Slot, WeightError, distribute

ACTIVE = "active"
REMOVED = "removed"
KT_ACTIVE = "active"
KT_DISABLED = "disabled"


class EditorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Link(Protocol):
    offer_id: int
    position: int
    status: str
    share: int
    pinned: bool
    kt_state: str
    pub_present: bool
    pub_share: int | None
    pub_state: str | None
    pub_pinned: bool
    ever_published: bool
    remote_link_id: int | None
    archived_at: datetime | None


@dataclass(frozen=True)
class RemoteOffer:
    """Связь offer↔stream, как её вернул Keitaro."""

    offer_id: int
    share: int
    state: str = KT_ACTIVE
    link_id: int | None = None


LinkFactory = Callable[[int, int], Link]  # (offer_id, position) -> новая связь


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ordered(links: Iterable[Link]) -> list[Link]:
    return sorted(links, key=lambda l: (l.position, l.offer_id))


def find(links: Iterable[Link], offer_id: int) -> Link | None:
    return next((l for l in links if l.offer_id == offer_id), None)


def _in_rotation(l: Link) -> bool:
    """Участвует в распределении: активна в черновике и не выключена в Keitaro."""
    return l.status == ACTIVE and l.kt_state == KT_ACTIVE


def _plan(links: Sequence[Link], override: dict[int, tuple[bool, bool, int]]) -> dict[int, int]:
    """Посчитать доли для будущего состояния.

    override: offer_id -> (в ротации?, pinned, pinned_share) для изменяемых связей.
    """
    slots: list[Slot] = []
    for l in ordered(links):
        active, pinned, share = override.get(l.offer_id, (_in_rotation(l), l.pinned, l.share))
        if active:
            slots.append(Slot(l.offer_id, pinned, share))
    try:
        return distribute(slots)
    except WeightError as e:
        raise EditorError(e.code, e.message) from e


def _apply(links: Sequence[Link], shares: dict[int, int]) -> None:
    for l in links:
        if l.offer_id in shares:
            l.share = shares[l.offer_id]


def _archive(l: Link) -> None:
    l.status = REMOVED
    l.share = 0
    l.pinned = False
    l.archived_at = _now()


def is_link_dirty(l: Link) -> bool:
    if l.pub_present:
        return l.status != ACTIVE or l.share != l.pub_share or l.kt_state != (l.pub_state or KT_ACTIVE)
    return l.status == ACTIVE


def is_dirty(links: Iterable[Link]) -> bool:
    return any(is_link_dirty(l) for l in links)


def sync_pin_baseline_if_clean(links: Sequence[Link]) -> None:
    """Pin — локальная настройка. Если черновик совпадает с опубликованным, pin становится базой для Cancel."""
    if not is_dirty(links):
        for l in links:
            l.pub_pinned = l.pinned


def add(links: list[Link], offer_id: int, factory: LinkFactory) -> Link:
    existing = find(links, offer_id)
    if existing is not None:
        if existing.status == ACTIVE:
            raise EditorError("already_in_stream", f"Оффер {offer_id} уже есть в потоке")
        return restore(links, offer_id)
    position = max((l.position for l in links), default=0) + 1
    new = factory(offer_id, position)
    new.status = ACTIVE
    new.kt_state = KT_ACTIVE
    new.pinned = False
    shares = _plan([*links, new], {})
    links.append(new)
    _apply(links, shares)
    return new


def remove(links: Sequence[Link], offer_id: int) -> Link:
    l = find(links, offer_id)
    if l is None or l.status != ACTIVE:
        raise EditorError("not_active", f"Оффер {offer_id} не активен в потоке")
    if _in_rotation(l) and sum(1 for x in links if _in_rotation(x)) == 1:
        raise EditorError(
            "last_active",
            "Нельзя убрать последний активный оффер: поток останется без назначения. Сначала добавьте другой.",
        )
    shares = _plan(links, {offer_id: (False, False, 0)})
    _archive(l)
    _apply(links, shares)
    return l


def restore(links: Sequence[Link], offer_id: int) -> Link:
    l = find(links, offer_id)
    if l is None or l.status != REMOVED:
        raise EditorError("not_removed", f"Оффер {offer_id} не находится в архиве потока")
    shares = _plan(links, {offer_id: (True, False, 0)})
    l.status = ACTIVE
    l.kt_state = KT_ACTIVE
    l.pinned = False
    l.archived_at = None
    _apply(links, shares)
    return l


def pin(links: Sequence[Link], offer_id: int, share: int | None = None) -> Link:
    l = find(links, offer_id)
    if l is None or not _in_rotation(l):
        raise EditorError("not_active", f"Оффер {offer_id} не активен в потоке")
    value = l.share if share is None else share
    shares = _plan(links, {offer_id: (True, True, value)})
    l.pinned = True
    l.share = value
    _apply(links, shares)
    return l


def unpin(links: Sequence[Link], offer_id: int) -> Link:
    l = find(links, offer_id)
    if l is None or not l.pinned:
        raise EditorError("not_pinned", f"Доля оффера {offer_id} не закреплена")
    shares = _plan(links, {offer_id: (True, False, 0)})
    l.pinned = False
    _apply(links, shares)
    return l


def cancel(links: Sequence[Link]) -> list[Link]:
    """Вернуть черновик к опубликованному. Возвращает связи, которые нужно удалить (никогда не публиковались)."""
    to_delete: list[Link] = []
    for l in links:
        if l.pub_present:
            l.status = ACTIVE
            l.share = l.pub_share or 0
            l.kt_state = l.pub_state or KT_ACTIVE
            l.pinned = l.pub_pinned
            l.archived_at = None
        elif l.ever_published:
            if l.status != REMOVED:
                _archive(l)
        else:
            to_delete.append(l)
    return to_delete


def desired_offers(links: Iterable[Link]) -> list[dict]:
    """Тело offers для PUT /streams/{id}: активные связи (включая выключенные в Keitaro) в порядке показа."""
    return [
        {"offer_id": l.offer_id, "share": l.share, "state": l.kt_state}
        for l in ordered(links)
        if l.status == ACTIVE
    ]


def has_duplicates(remote: Iterable[RemoteOffer]) -> list[int]:
    seen: set[int] = set()
    dups: list[int] = []
    for r in remote:
        if r.offer_id in seen:
            dups.append(r.offer_id)
        seen.add(r.offer_id)
    return dups


def _remote_rows(remote: Iterable[RemoteOffer]) -> list[tuple[int, int, str]]:
    # Список, а не множество: две одинаковые связи не должны схлопываться в одну.
    return sorted((r.offer_id, r.share, r.state) for r in remote)


def desired_matches(links: Iterable[Link], remote: Iterable[RemoteOffer]) -> bool:
    remote = list(remote)
    if has_duplicates(remote):
        return False
    want = sorted((o["offer_id"], o["share"], o["state"]) for o in desired_offers(links))
    return want == _remote_rows(remote)


def published_matches(links: Iterable[Link], remote: Iterable[RemoteOffer]) -> bool:
    remote = list(remote)
    if has_duplicates(remote):
        return False
    known = sorted((l.offer_id, l.pub_share or 0, l.pub_state or KT_ACTIVE) for l in links if l.pub_present)
    return known == _remote_rows(remote)


@dataclass
class MergeResult:
    added: list[int] = field(default_factory=list)
    removed_externally: list[int] = field(default_factory=list)


def merge_remote(links: list[Link], remote: Iterable[RemoteOffer], factory: LinkFactory) -> MergeResult:
    """Применить прочитанное из Keitaro к чистому потоку (черновик должен быть сброшен заранее).

    Опубликованное и черновик ← Keitaro. Доли не пересчитываются. Известные связи, которых больше
    нет в Keitaro, уходят в локальный архив и не теряются.
    """
    remote_list = list(remote)
    dups = has_duplicates(remote_list)
    if dups:
        raise EditorError("remote_duplicates", f"Keitaro вернул повторяющиеся связи с офферами {sorted(set(dups))}")
    result = MergeResult()
    by_offer = {r.offer_id: r for r in remote_list}
    next_pos = max((l.position for l in links), default=0)

    for l in links:
        r = by_offer.get(l.offer_id)
        if r is None:
            if l.pub_present:
                result.removed_externally.append(l.offer_id)
            if l.pub_present or l.status == ACTIVE:
                l.pub_present = False
                l.pub_share = None
                l.pub_state = None
                _archive(l)
            continue
        l.pub_present = True
        l.pub_share = r.share
        l.pub_state = r.state
        l.ever_published = True
        l.remote_link_id = r.link_id
        l.status = ACTIVE
        l.share = r.share
        l.kt_state = r.state
        l.archived_at = None

    known = {l.offer_id for l in links}
    for r in remote_list:
        if r.offer_id in known:
            continue
        next_pos += 1
        l = factory(r.offer_id, next_pos)
        l.pub_present = True
        l.pub_share = r.share
        l.pub_state = r.state
        l.ever_published = True
        l.remote_link_id = r.link_id
        l.status = ACTIVE
        l.share = r.share
        l.kt_state = r.state
        l.pinned = False
        links.append(l)
        result.added.append(r.offer_id)

    for l in links:
        l.pub_pinned = l.pinned
    return result


def mark_published(links: Sequence[Link], remote: Iterable[RemoteOffer]) -> None:
    """После проверенного Push: опубликованное ← черновик (подтверждено чтением remote)."""
    remote = list(remote)
    if has_duplicates(remote):
        raise EditorError("remote_duplicates", "Keitaro вернул повторяющиеся связи — публикация не подтверждена")
    by_offer = {r.offer_id: r for r in remote}
    for l in links:
        r = by_offer.get(l.offer_id)
        if r is not None and l.status == ACTIVE:
            l.pub_present = True
            l.pub_share = r.share
            l.pub_state = r.state
            l.ever_published = True
            l.remote_link_id = r.link_id
        else:
            l.pub_present = False
            l.pub_share = None
            l.pub_state = None
            if l.status == ACTIVE:  # не должно случаться: desired_matches проверен до вызова
                raise EditorError("verify_mismatch", "Опубликованный состав не совпал с черновиком")
        l.pub_pinned = l.pinned
