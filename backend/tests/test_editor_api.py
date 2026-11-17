"""Интеграционные сценарии редактора: HTTP API + SQLite (миграции) + фейковый Keitaro через httpx-транспорт."""

import httpx

from app.devtools.fake_keitaro import Fault
from app.keitaro.schemas import protected_view

CAMPAIGN, GEO_STREAM, OFFER_STREAM = 5000, 5001, 5002


def fetch(api, discard=False):
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": discard})
    assert r.status_code == 200, r.text
    return r.json()["campaign"]


def stream(campaign, sid=OFFER_STREAM):
    return next(s for s in campaign["streams"] if s["id"] == sid)


def shares(s):
    return [(l["offer_id"], l["status"], l["share"], l["pinned"]) for l in s["links"]]


def op(api, s, offer_id, action, **extra):
    r = api.post(f"/api/streams/{s['id']}/offers/{offer_id}/{action}", json={"expected_version": s["draft_version"], **extra})
    assert r.status_code == 200, r.text
    return r.json()


def add(api, s, offer_id):
    r = api.post(f"/api/streams/{s['id']}/offers", json={"expected_version": s["draft_version"], "offer_id": offer_id})
    assert r.status_code == 200, r.text
    return r.json()


def push(api, s, expect=200):
    r = api.post(f"/api/streams/{s['id']}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == expect, r.text
    return r.json()


def test_video_scenario_end_to_end(api, fake):
    c = fetch(api)
    geo = stream(c, GEO_STREAM)
    assert geo["has_offers"] is False and geo["links"] == []  # поток без офферов — просто поток
    s = stream(c)
    assert shares(s) == [(3749, "active", 33, False), (3717, "active", 33, False), (11111, "active", 34, False)]
    assert s["links"][0]["offer_name"].startswith("3749")

    # Add → 4×25, dirty, Keitaro не изменён
    s = add(api, s, 11112)
    assert [l["share"] for l in s["links"]] == [25, 25, 25, 25] and s["dirty"]
    assert s["links"][-1]["view_state"] == "new"
    assert fake.stream_offers(OFFER_STREAM) == [(3749, 33, "active"), (3717, 33, "active"), (11111, 34, "active")]

    # Push → Keitaro = черновик, чисто
    res = push(api, s)
    assert res["result"] == "published" and not res["stream"]["dirty"]
    assert fake.stream_offers(OFFER_STREAM) == [(3749, 25, "active"), (3717, 25, "active"), (11111, 25, "active"), (11112, 25, "active")]
    s = res["stream"]

    # Remove → серый 0%, dirty → Push → исчез из Keitaro, остался в архиве
    s = op(api, s, 3717, "remove")
    removed = next(l for l in s["links"] if l["offer_id"] == 3717)
    assert (removed["status"], removed["share"], removed["view_state"]) == ("removed", 0, "pending_remove") and s["dirty"]
    s = push(api, s)["stream"]
    assert 3717 not in [o for o, _, _ in fake.stream_offers(OFFER_STREAM)]
    assert next(l for l in s["links"] if l["offer_id"] == 3717)["view_state"] == "archived" and not s["dirty"]

    # Bring back → dirty → Push → снова в Keitaro
    s = op(api, s, 3717, "restore")
    assert s["dirty"]
    s = push(api, s)["stream"]
    assert 3717 in [o for o, _, _ in fake.stream_offers(OFFER_STREAM)]

    # Внешнее удаление в Keitaro + Fetch → removed/архив, Bring back доступен
    fake.external_remove_offer(OFFER_STREAM, 3749)
    fake.external_remove_offer(OFFER_STREAM, 11111)
    s = stream(fetch(api))
    assert {l["offer_id"]: l["view_state"] for l in s["links"]}[3749] == "archived"
    assert {l["offer_id"]: l["view_state"] for l in s["links"]}[11111] == "archived"
    assert not s["dirty"]

    # Pin 25%: последующий Bring back пересчитывает только незакреплённых
    s = op(api, s, 11112, "pin", share=25)
    s = op(api, s, 3749, "restore")
    active = [(l["offer_id"], l["share"], l["pinned"]) for l in s["links"] if l["status"] == "active"]
    assert (11112, 25, True) in active and sum(x[1] for x in active) == 100
    assert sorted(x[1] for x in active if not x[2]) == [37, 38]
    s = push(api, s)["stream"]
    assert sum(sh for _, sh, _ in fake.stream_offers(OFFER_STREAM)) == 100
    assert (11112, 25, "active") in fake.stream_offers(OFFER_STREAM)


def test_cancel_drops_new_link_but_keeps_global_offer(api, fake):
    s = stream(fetch(api))
    s = add(api, s, 11112)
    r = api.post(f"/api/streams/{OFFER_STREAM}/cancel", json={"expected_version": s["draft_version"]})
    s = r.json()
    assert shares(s) == [(3749, "active", 33, False), (3717, "active", 33, False), (11111, "active", 34, False)]
    assert not s["dirty"]
    assert 11112 in fake.offers  # глобальный оффер не тронут
    assert [o["id"] for o in api.get("/api/offers", params={"q": "11112"}).json()] == [11112]


def test_push_never_touches_other_stream_fields(api, fake):
    before = {sid: protected_view(fake.streams[sid]) for sid in (GEO_STREAM, OFFER_STREAM)}
    s = add(api, stream(fetch(api)), 11112)
    push(api, s)
    after = {sid: protected_view(fake.streams[sid]) for sid in (GEO_STREAM, OFFER_STREAM)}
    assert after == before
    assert [m for m, p in fake.calls if m != "GET"] == ["PUT"]


def test_fetch_with_dirty_draft_returns_conflict_then_discards_on_confirm(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={})
    assert r.status_code == 409 and r.json()["code"] == "dirty_drafts" and r.json()["streams"] == [OFFER_STREAM]
    assert stream(api.get(f"/api/campaigns/{CAMPAIGN}").json())["dirty"]  # черновик не затёрт
    s = stream(fetch(api, discard=True))
    assert not s["dirty"] and 11112 not in [l["offer_id"] for l in s["links"]]


def test_repeated_fetch_creates_no_duplicates(api):
    fetch(api)
    fetch(api)
    s = stream(fetch(api))
    assert len(s["links"]) == 3


def test_stale_version_is_rejected_and_does_not_overwrite(api):
    s = stream(fetch(api))
    old = dict(s)
    s = add(api, s, 11112)
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers/3749/remove", json={"expected_version": old["draft_version"]})
    assert r.status_code == 409 and r.json()["code"] == "stale_version"
    current = stream(api.get(f"/api/campaigns/{CAMPAIGN}").json())
    assert [l["share"] for l in current["links"]] == [25, 25, 25, 25]
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": old["draft_version"]})
    assert r.status_code == 409


def test_add_unknown_or_unavailable_offer(api):
    s = stream(fetch(api))
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers", json={"expected_version": s["draft_version"], "offer_id": 424242})
    assert r.status_code == 422 and r.json()["code"] == "offer_not_found"
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers", json={"expected_version": s["draft_version"], "offer_id": 999})
    assert r.status_code == 422 and r.json()["code"] == "offer_unavailable"
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers", json={"expected_version": s["draft_version"], "offer_id": 3749})
    assert r.status_code == 409 and r.json()["code"] == "already_in_stream"


def test_pin_errors_are_reported(api):
    s = stream(fetch(api))
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers/3749/pin", json={"expected_version": s["draft_version"], "share": 100})
    assert r.status_code == 409 and r.json()["code"] == "no_free_share"
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers/3749/pin", json={"expected_version": s["draft_version"], "share": 101})
    assert r.status_code == 422


def test_push_timeout_after_apply_is_reconciled_as_success(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    fake.faults.append(Fault("PUT", r"/streams/\d+", "timeout_after"))
    res = push(api, s)
    assert res["result"] == "published" and not res["stream"]["dirty"]


def test_push_failure_keeps_draft_and_retry_does_not_duplicate(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    fake.faults.append(Fault("PUT", r"/streams/\d+", "error_500_before"))
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 502 and r.json()["code"] == "push_unconfirmed"
    s = stream(api.get(f"/api/campaigns/{CAMPAIGN}").json())
    assert s["dirty"] and s["push_state"] == "idle" and s["last_error"]
    assert len(fake.stream_offers(OFFER_STREAM)) == 3
    push(api, s)
    assert len(fake.stream_offers(OFFER_STREAM)) == 4  # ровно один набор, без дублей


def test_push_connect_timeout_is_clean_error(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    fake.faults.append(Fault("PUT", r"/streams/\d+", "timeout_before"))
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 502 and r.json()["code"] == "keitaro_timeout"
    assert stream(api.get(f"/api/campaigns/{CAMPAIGN}").json())["dirty"]


def test_unconfirmed_push_then_retry_does_not_write_twice(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    fake.faults.append(Fault("PUT", r"/streams/\d+", "drop_after"))
    fake.faults.append(Fault("GET", rf"/streams/{OFFER_STREAM}", "timeout_before"))  # первое чтение перед PUT упадёт
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 502  # чтение перед PUT не удалось — PUT не выполнялся
    assert [m for m, _ in fake.calls if m == "PUT"] == []
    s = stream(api.get(f"/api/campaigns/{CAMPAIGN}").json())
    # теперь PUT применится, но соединение оборвётся; проверка чтением подтвердит
    res = push(api, s)
    assert res["result"] == "published"
    puts = [m for m, _ in fake.calls if m == "PUT"]
    assert len(puts) == 1
    # повторный Push чистого потока ничего не пишет
    assert push(api, res["stream"])["result"] == "nothing_to_push"
    assert len([m for m, _ in fake.calls if m == "PUT"]) == 1


def test_verify_read_failure_then_retry_detects_already_applied(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    # 1-й GET (до PUT) проходит, PUT проходит, 2-й GET (проверка) падает
    original = fake.mock_handler
    state = {"gets": 0}

    def flaky(request):
        if request.method == "GET" and request.url.path.endswith(f"/streams/{OFFER_STREAM}"):
            state["gets"] += 1
            if state["gets"] == 2:
                raise httpx.ConnectError("down", request=request)
        return original(request)

    fake.mock_handler = flaky
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 502 and r.json()["code"] == "push_unconfirmed"
    s = stream(api.get(f"/api/campaigns/{CAMPAIGN}").json())
    assert s["dirty"]  # не показываем ложный успех
    res = push(api, s)
    assert res["result"] == "already_applied" and not res["stream"]["dirty"]
    assert len([m for m, _ in fake.calls if m == "PUT"]) == 1


def test_external_change_before_push_is_conflict(api, fake):
    s = add(api, stream(fetch(api)), 11112)
    fake.external_set_offers(OFFER_STREAM, [(3749, 100)])
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 409 and r.json()["code"] == "remote_changed"
    assert fake.stream_offers(OFFER_STREAM) == [(3749, 100, "active")]


def test_push_is_forbidden_for_foreign_campaign(api, fake):
    foreign = fake._create_campaign({"alias": "other", "name": "test_tolmakov_campaign1", "group_id": 9799})
    st = fake._create_stream({"campaign_id": foreign["id"], "type": "regular", "name": "Offers", "schema": "landings",
                              "action_type": "http", "offers": [{"offer_id": 3749, "share": 100}]})
    r = api.post(f"/api/campaigns/{foreign['id']}/fetch", json={})
    c = r.json()["campaign"]
    assert c["push_allowed"] is False
    s = c["streams"][0]
    s = add(api, s, 3717)
    r = api.post(f"/api/streams/{st['id']}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 403 and r.json()["code"] == "push_forbidden"
    assert [m for m, _ in fake.calls if m == "PUT"] == []


def test_edits_blocked_while_pushing(api):
    from datetime import datetime, timezone

    from app.models import Stream

    s = add(api, stream(fetch(api)), 11112)
    db = api.app.state.db
    with db.sessions() as session:
        row = session.get(Stream, OFFER_STREAM)
        row.push_state, row.push_started_at = "pushing", datetime.now(timezone.utc)
        session.commit()
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers/3749/remove", json={"expected_version": s["draft_version"]})
    assert r.status_code == 423
    r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": s["draft_version"]})
    assert r.status_code == 423


def test_auth_error_is_reported_without_secret(api, fake):
    fake.faults.append(Fault("GET", rf"/campaigns/{CAMPAIGN}", "auth"))
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={})
    assert r.status_code == 502 and r.json()["code"] == "keitaro_auth"
    assert "fake-key" not in r.text


def test_external_removal_of_stream_marks_missing(api, fake):
    fetch(api)
    del fake.streams[GEO_STREAM]
    c = fetch(api)
    assert stream(c, GEO_STREAM)["remote_missing"] is True
