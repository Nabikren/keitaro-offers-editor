"""Состояния черновика: Add/Remove/Bring back/Pin/Cancel/Fetch-merge/Push-verify без БД и сети."""

from dataclasses import dataclass
from datetime import datetime

import pytest

from app.domain import links as L
from app.domain.links import EditorError, RemoteOffer


@dataclass
class Link:
    offer_id: int
    position: int
    status: str = "active"
    share: int = 0
    pinned: bool = False
    kt_state: str = "active"
    pub_present: bool = False
    pub_share: int | None = None
    pub_state: str | None = None
    pub_pinned: bool = False
    ever_published: bool = False
    remote_link_id: int | None = None
    archived_at: datetime | None = None


def factory(offer_id, position):
    return Link(offer_id, position)


def fetched(*pairs):
    """Поток, только что прочитанный из Keitaro: [(offer_id, share), ...]."""
    links: list[Link] = []
    L.merge_remote(links, [RemoteOffer(o, s, "active", 1000 + o) for o, s in pairs], factory)
    return links


def view(links):
    return [(l.offer_id, l.status, l.share, l.pinned) for l in L.ordered(links)]


def publish(links):
    """Имитация успешного Push: Keitaro теперь хранит желаемый состав."""
    remote = [RemoteOffer(o["offer_id"], o["share"], o["state"]) for o in L.desired_offers(links)]
    assert L.desired_matches(links, remote)
    L.mark_published(links, remote)
    return remote


def test_fetch_keeps_keitaro_shares_without_recalc():
    links = fetched((1, 33), (2, 33), (3, 34))
    assert view(links) == [(1, "active", 33, False), (2, "active", 33, False), (3, "active", 34, False)]
    assert not L.is_dirty(links)


def test_add_recalculates_and_marks_dirty_like_video():
    links = fetched((1, 33), (2, 33), (3, 34))
    L.add(links, 4, factory)
    assert [s for _, _, s, _ in view(links)] == [25, 25, 25, 25]
    assert L.is_dirty(links)
    # Keitaro ещё не тронут: опубликованное прежнее
    assert [(l.offer_id, l.pub_share) for l in links if l.pub_present] == [(1, 33), (2, 33), (3, 34)]


def test_add_existing_active_is_rejected_without_duplicates():
    links = fetched((1, 100))
    with pytest.raises(EditorError) as e:
        L.add(links, 1, factory)
    assert e.value.code == "already_in_stream"
    assert len(links) == 1


def test_push_then_clean():
    links = fetched((1, 50), (2, 50))
    L.add(links, 3, factory)
    publish(links)
    assert not L.is_dirty(links)
    assert all(l.ever_published for l in links)


def test_cancel_drops_unpublished_link_and_restores_shares():
    links = fetched((1, 33), (2, 33), (3, 34))
    L.add(links, 4, factory)
    to_delete = L.cancel(links)
    assert [l.offer_id for l in to_delete] == [4]
    links = [l for l in links if l not in to_delete]
    assert view(links) == [(1, "active", 33, False), (2, "active", 33, False), (3, "active", 34, False)]
    assert not L.is_dirty(links)


def test_remove_greys_out_with_zero_and_dirty_then_archive_after_push():
    links = fetched((1, 50), (2, 50))
    L.add(links, 3, factory)
    publish(links)
    L.remove(links, 2)
    assert view(links) == [(1, "active", 50, False), (2, "removed", 0, False), (3, "active", 50, False)]
    assert L.is_dirty(links)
    remote = publish(links)
    assert {r.offer_id for r in remote} == {1, 3}
    assert not L.is_dirty(links)
    archived = L.find(links, 2)
    assert archived.status == "removed" and not archived.pub_present and archived.ever_published


def test_bring_back_from_archive_is_draft_until_push():
    links = fetched((1, 50), (2, 50))
    L.remove(links, 2)
    publish(links)
    L.restore(links, 2)
    assert view(links) == [(1, "active", 50, False), (2, "active", 50, False)]
    assert L.is_dirty(links)
    publish(links)
    assert not L.is_dirty(links)


def test_cancel_of_pending_remove_and_of_bring_back():
    links = fetched((1, 50), (2, 50))
    L.remove(links, 2)
    assert L.cancel(links) == []
    assert view(links) == [(1, "active", 50, False), (2, "active", 50, False)]
    L.remove(links, 2)
    publish(links)
    L.restore(links, 2)
    assert L.cancel(links) == []  # связь была опубликована раньше → возвращается в архив, не удаляется
    assert view(links)[1] == (2, "removed", 0, False)
    assert not L.is_dirty(links)


def test_add_of_archived_offer_is_bring_back():
    links = fetched((1, 50), (2, 50))
    L.remove(links, 2)
    publish(links)
    L.add(links, 2, factory)
    assert len(links) == 2 and L.find(links, 2).status == "active"


def test_remove_last_active_is_rejected():
    links = fetched((1, 100))
    with pytest.raises(EditorError) as e:
        L.remove(links, 1)
    assert e.value.code == "last_active"
    assert view(links) == [(1, "active", 100, False)]


def test_external_delete_then_fetch_archives_known_links():
    links = fetched((1, 25), (2, 25), (3, 25), (4, 25))
    result = L.merge_remote(links, [RemoteOffer(1, 50), RemoteOffer(3, 50)], factory)
    assert sorted(result.removed_externally) == [2, 4]
    assert view(links) == [(1, "active", 50, False), (2, "removed", 0, False), (3, "active", 50, False), (4, "removed", 0, False)]
    assert not L.is_dirty(links)
    # Bring back доступен для внешне удалённых
    L.restore(links, 4)
    assert [s for _, st, s, _ in view(links) if st == "active"] == [34, 33, 33]


def test_repeated_fetch_is_idempotent():
    links = fetched((1, 50), (2, 50))
    before = view(links)
    L.merge_remote(links, [RemoteOffer(1, 50), RemoteOffer(2, 50)], factory)
    L.merge_remote(links, [RemoteOffer(1, 50), RemoteOffer(2, 50)], factory)
    assert view(links) == before and len(links) == 2


def test_fetch_new_remote_offer_appended_after_known():
    links = fetched((1, 100))
    result = L.merge_remote(links, [RemoteOffer(1, 60), RemoteOffer(9, 40)], factory)
    assert result.added == [9]
    assert view(links) == [(1, "active", 60, False), (9, "active", 40, False)]


@pytest.mark.parametrize("dups", [
    [RemoteOffer(1, 50), RemoteOffer(1, 50)],             # одинаковые дубли
    [RemoteOffer(1, 50), RemoteOffer(1, 30, "disabled")],  # различающиеся дубли
])
def test_duplicate_remote_links_are_explicit_conflict(dups):
    links = fetched((1, 50), (2, 50))
    before = view(links)
    with pytest.raises(EditorError) as e:
        L.merge_remote(links, dups + [RemoteOffer(2, 50)], factory)
    assert e.value.code == "remote_duplicates"
    assert view(links) == before  # ничего не применено


def test_duplicates_never_confirm_push_or_published_state():
    links = fetched((1, 50), (2, 50))
    # Множество {(1,50),(2,50)} совпало бы, но связей в Keitaro три — это не «чисто».
    remote = [RemoteOffer(1, 50), RemoteOffer(1, 50), RemoteOffer(2, 50)]
    assert not L.desired_matches(links, remote)
    assert not L.published_matches(links, remote)
    with pytest.raises(EditorError):
        L.mark_published(links, remote)


def test_pin_flow_like_video():
    # 4 оффера по 25, pin третьего, затем Bring back архивного → закреплённый остаётся 25.
    links = fetched((1, 25), (2, 25), (3, 25), (4, 25))
    L.remove(links, 4)
    publish(links)
    L.pin(links, 3)
    assert L.find(links, 3).share == 33 and L.find(links, 3).pinned  # pin фиксирует текущую долю (34/33/33)
    L.unpin(links, 3)
    L.pin(links, 3, 25)
    assert [s for _, st, s, _ in view(links) if st == "active"] == [38, 37, 25]
    L.restore(links, 4)
    assert [(o, s, p) for o, st, s, p in view(links) if st == "active"] == [(1, 25, False), (2, 25, False), (3, 25, True), (4, 25, False)]
    L.remove(links, 1)
    assert [(o, s) for o, st, s, p in view(links) if st == "active"] == [(2, 38), (3, 25), (4, 37)]


def test_pin_over_100_rejected_and_state_untouched():
    links = fetched((1, 50), (2, 25), (3, 25))
    L.pin(links, 1, 60)
    before = view(links)
    with pytest.raises(EditorError) as e:
        L.pin(links, 2, 50)
    assert e.value.code == "pinned_over_100"
    assert view(links) == before


def test_pin_100_with_others_rejected():
    links = fetched((1, 50), (2, 50))
    with pytest.raises(EditorError) as e:
        L.pin(links, 1, 100)
    assert e.value.code == "no_free_share"


def test_adding_when_all_pinned_100_is_rejected():
    links = fetched((1, 60), (2, 40))
    L.pin(links, 1, 60)
    L.pin(links, 2, 40)
    with pytest.raises(EditorError) as e:
        L.add(links, 3, factory)
    assert e.value.code == "no_free_share"
    assert len(links) == 2


def test_removing_unpinned_when_rest_pinned_not_100_is_rejected():
    links = fetched((1, 40), (2, 30), (3, 30))
    L.pin(links, 1, 40)
    L.pin(links, 2, 30)
    with pytest.raises(EditorError) as e:
        L.remove(links, 3)
    assert e.value.code == "pinned_not_100"


def test_remove_clears_pin_so_restore_cannot_conflict():
    links = fetched((1, 40), (2, 30), (3, 30))
    L.pin(links, 2, 30)
    L.remove(links, 2)
    assert not L.find(links, 2).pinned
    L.pin(links, 1, 80)
    L.restore(links, 2)  # вернулся незакреплённым: 80 закреплено, 20 делят двое
    assert [(o, s, p) for o, st, s, p in view(links) if st == "active"] == [(1, 80, True), (2, 10, False), (3, 10, False)]


def test_cancel_restores_pins_from_last_published_baseline():
    links = fetched((1, 50), (2, 50))
    L.pin(links, 1, 50)  # доли не изменились → поток чистый, pin становится базой
    L.sync_pin_baseline_if_clean(links)
    L.add(links, 3, factory)
    L.unpin(links, 1)
    L.cancel(links)
    assert L.find(links, 1).pinned is True


def test_disabled_in_keitaro_is_preserved_and_not_redistributed():
    links: list[Link] = []
    L.merge_remote(links, [RemoteOffer(1, 50), RemoteOffer(2, 50, "disabled")], factory)
    L.add(links, 3, factory)
    assert [(o["offer_id"], o["share"], o["state"]) for o in L.desired_offers(links)] == [
        (1, 50, "active"), (2, 50, "disabled"), (3, 50, "active")]


def test_published_matches_detects_external_change():
    links = fetched((1, 50), (2, 50))
    assert L.published_matches(links, [RemoteOffer(1, 50), RemoteOffer(2, 50)])
    assert not L.published_matches(links, [RemoteOffer(1, 100)])


def test_desired_payload_excludes_removed_and_contains_no_read_only_fields():
    links = fetched((1, 50), (2, 50))
    L.add(links, 3, factory)
    L.remove(links, 2)
    payload = L.desired_offers(links)
    assert payload == [{"offer_id": 1, "share": 50, "state": "active"}, {"offer_id": 3, "share": 50, "state": "active"}]
    assert sum(o["share"] for o in payload) == 100
