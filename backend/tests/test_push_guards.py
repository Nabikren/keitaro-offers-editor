"""Hardening Push: префикс, проверка кампании/alias перед PUT, защищённые поля, гонки Fetch/Edit/Push, дубли связей."""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.devtools.fake_keitaro import API_KEY
from app.keitaro.client import KeitaroClient
from app.keitaro.schemas import changed_protected_fields
from app.main import create_app
from app.models import Stream
from app.services.editor import EditorService

CAMPAIGN, GEO_STREAM, OFFER_STREAM = 5000, 5001, 5002


def make_api(tmp_path, fake, prefix):
    settings = Settings(_env_file=None, keitaro_url="https://kt.test", keitaro_api_key=API_KEY,
                        database_url=f"sqlite:///{tmp_path / f'{uuid.uuid4().hex}.sqlite3'}", campaign_name_prefix=prefix)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    app = create_app(settings, kt_factory=lambda: KeitaroClient(settings.keitaro_url, API_KEY, 2, transport=fake.transport()))
    return TestClient(app), settings


def fetch(api, campaign=CAMPAIGN, discard=False):
    r = api.post(f"/api/campaigns/{campaign}/fetch", json={"discard_drafts": discard})
    assert r.status_code == 200, r.text
    return r.json()["campaign"]


def offer_stream(c):
    return next(s for s in c["streams"] if s["links"])


def add(api, s, offer_id):
    r = api.post(f"/api/streams/{s['id']}/offers", json={"expected_version": s["draft_version"], "offer_id": offer_id})
    assert r.status_code == 200, r.text
    return r.json()


def push(api, s):
    return api.post(f"/api/streams/{s['id']}/push", json={"expected_version": s["draft_version"]})


def puts(fake):
    return [p for m, p in fake.calls if m == "PUT"]


def current(api, sid=OFFER_STREAM, campaign=CAMPAIGN):
    return next(s for s in api.get(f"/api/campaigns/{campaign}").json()["streams"] if s["id"] == sid)


# --- 1. префикс, кампания, alias ---

@pytest.mark.parametrize("prefix", ["", "   ", " test_simonov_", "te", "test simonov_", "test_simonov_/",
                                    "prod_", "test_other_", "test_simonov", "TEST_SIMONOV_", "x_test_simonov_"])
def test_empty_or_invalid_prefix_forbids_push(tmp_path, fake, prefix):
    api, _ = make_api(tmp_path, fake, prefix)
    with api:
        c = fetch(api)
        assert c["push_allowed"] is False
        s = add(api, offer_stream(c), 11112)
        r = push(api, s)
        assert r.status_code == 403 and r.json()["code"] == "push_forbidden"
        assert puts(fake) == []


def test_empty_prefix_forbids_campaign_creation(tmp_path, fake):
    api, _ = make_api(tmp_path, fake, "  ")
    with api:
        r = api.post("/api/campaigns", json={"idempotency_key": uuid.uuid4().hex, "name": "x", "geo": "AU", "offer_id": 10184,
                                             "domain_id": 1813, "group_id": 10332, "traffic_source_id": 836})
        assert r.status_code == 403 and r.json()["code"] == "prefix_not_configured"
        assert [c for c in fake.calls if c[0] != "GET"] == []


def test_campaign_renamed_in_keitaro_after_fetch_blocks_put(api, fake):
    s = add(api, offer_stream(fetch(api)), 11112)
    fake.campaigns[CAMPAIGN]["name"] = "someone_else_campaign"
    r = push(api, s)
    assert r.status_code == 403 and r.json()["code"] == "push_forbidden"
    assert puts(fake) == []
    st = current(api)
    assert st["dirty"] and st["push_state"] == "idle" and "someone_else_campaign" in st["last_error"]


def test_remote_stream_moved_to_other_campaign_blocks_put(api, fake):
    s = add(api, offer_stream(fetch(api)), 11112)
    fake.streams[OFFER_STREAM]["campaign_id"] = 7777
    r = push(api, s)
    assert r.status_code == 409 and r.json()["code"] == "remote_campaign_mismatch"
    assert puts(fake) == []
    assert current(api)["push_state"] == "idle"


def test_changed_alias_of_created_campaign_blocks_put(api, fake):
    job = api.post("/api/campaigns", json={"idempotency_key": uuid.uuid4().hex, "name": "alias-check", "geo": "AU", "offer_id": 10184,
                                           "domain_id": 1813, "group_id": 10332, "traffic_source_id": 836}).json()
    assert job["status"] == "done"
    s = add(api, offer_stream(fetch(api, job["campaign_id"])), 3749)
    fake.campaigns[job["campaign_id"]]["alias"] = "replaced-alias"
    r = push(api, s)
    assert r.status_code == 409 and r.json()["code"] == "alias_mismatch"
    assert puts(fake) == []


def test_prefix_root_is_required():
    from app.config import Settings
    assert Settings(_env_file=None, campaign_name_prefix="test_simonov_").effective_prefix == "test_simonov_"
    assert Settings(_env_file=None, campaign_name_prefix="test_simonov_smoke_7f3k2q_").effective_prefix == "test_simonov_smoke_7f3k2q_"
    assert Settings(_env_file=None, campaign_name_prefix="test_tolmakov_").effective_prefix is None


def test_unique_smoke_prefix_is_supported(tmp_path, fake):
    prefix = "test_simonov_smoke_ab12cd_"
    api, _ = make_api(tmp_path, fake, prefix)
    with api:
        job = api.post("/api/campaigns", json={"idempotency_key": uuid.uuid4().hex, "name": "run1", "geo": "AU", "offer_id": 10184,
                                               "domain_id": 1813, "group_id": 10332, "traffic_source_id": 836}).json()
        assert job["status"] == "done" and job["name"] == "test_simonov_smoke_ab12cd_run1"
        c = fetch(api, job["campaign_id"])
        assert c["push_allowed"] is True
        assert push(api, add(api, offer_stream(c), 3749)).status_code == 200
        # обычная тестовая кампания без smoke-префикса теперь недоступна для Push
        demo = fetch(api)
        assert demo["push_allowed"] is False
        assert push(api, add(api, offer_stream(demo), 11112)).status_code == 403


# --- 2. защищённые поля ---

SIDE_EFFECTS = {
    "filters": [{"name": "country", "mode": "reject", "payload": ["RU"]}],
    "action_payload": "https://example.org/",
    "landings": [{"landing_id": 77, "share": 100, "state": "active"}],
    "triggers": [{"condition": "always", "action": "disable", "target": "stream", "interval": 60}],
    "collect_clicks": False,
    "offer_selection": "after_click",
    "weight": 5,
    "comments": "changed",
    "action_options": {"url": "x"},
    "type": "forced",
    "schema": "redirect",
    "state": "disabled",
    "filter_or": True,
    "campaign_id": 7777,
    "name": "Renamed by PUT",
    "position": 9,
    "action_type": "location",
}


def test_side_effect_matrix_covers_every_protected_field():
    from app.keitaro.schemas import PROTECTED_STREAM_FIELDS
    assert set(SIDE_EFFECTS) == set(PROTECTED_STREAM_FIELDS) and len(PROTECTED_STREAM_FIELDS) == 17


@pytest.mark.parametrize("field", list(SIDE_EFFECTS))
def test_protected_field_change_is_reported_and_blocks_next_push(api, fake, field):
    s = add(api, offer_stream(fetch(api)), 11112)
    fake.put_side_effects = {field: SIDE_EFFECTS[field]}
    r = push(api, s)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "side_effect_detected"
    assert body["changed_fields"] == [field]
    assert field in body["warning"]
    st = body["stream"]
    assert not st["dirty"]  # подтверждённый readback офферов сохранён
    assert st["side_effect_fields"] == [field]
    assert [o for o, _, _ in fake.stream_offers(OFFER_STREAM)] == [3749, 3717, 11111, 11112]
    # дальнейший Push этого потока остановлен до явного Fetch
    fake.put_side_effects = {}
    s2 = add(api, st, 24800)
    r2 = push(api, s2)
    assert r2.status_code == 409 and r2.json()["code"] == "side_effect_unresolved"
    assert len(puts(fake)) == 1
    st = offer_stream(fetch(api, discard=True))
    if field == "campaign_id":
        assert st["remote_missing"] is True  # поток «ушёл» в другую кампанию — правки и Push недоступны
    else:
        assert st["side_effect_fields"] == []


def test_protected_comparison_ignores_service_ids_order_and_empty_forms():
    before = {"type": "regular", "weight": 0, "action_options": None, "comments": None,
              "filters": [{"id": 1, "stream_id": 9, "oid": 1, "name": "country", "mode": "accept", "payload": ["MX", "AU"]},
                          {"id": 2, "name": "language", "mode": "accept", "payload": ["es"]}],
              "landings": [{"id": 5, "landing_id": 1, "share": 50, "state": "active", "created_at": "a", "updated_at": "b"}],
              "triggers": [{"id": 3, "oid": 3, "condition": "always", "action": "disable", "next_run_at": 100}]}
    after = {"type": "regular", "weight": 0.0, "action_options": {}, "comments": "",
             "filters": [{"id": 8, "name": "language", "mode": "accept", "payload": "es"},
                         {"id": 7, "stream_id": 9, "name": "country", "mode": "accept", "payload": "AU,MX"}],
             "landings": [{"id": 6, "landing_id": 1, "share": 50, "state": "active", "created_at": "c", "updated_at": "d"}],
             "triggers": [{"id": 4, "oid": 4, "condition": "always", "action": "disable", "next_run_at": 200}]}
    assert changed_protected_fields(before, after) == []
    assert changed_protected_fields(before, dict(after, landings=[])) == ["landings"]
    assert changed_protected_fields(before, dict(after, filters=after["filters"][:1])) == ["filters"]
    assert changed_protected_fields({"collect_clicks": True}, {"collect_clicks": False}) == ["collect_clicks"]
    assert changed_protected_fields({"name": "Flow"}, {}) == ["name"]  # пропавшее поле — изменение


# --- 4. гонки Fetch / Edit / Push ---

def _side_edit(api, settings, op, offer_id):
    """Правка «из другого запроса» через отдельную сессию БД."""
    with api.app.state.db.sessions() as session:
        version = session.get(Stream, OFFER_STREAM).draft_version
        EditorService(session, None, settings).edit(OFFER_STREAM, version, op, offer_id=offer_id)


def test_edit_between_fetch_read_and_apply_is_not_overwritten(api, fake, settings):
    fetch(api)
    fake.hooks.append(("GET", rf"/campaigns/{CAMPAIGN}/streams", lambda: _side_edit(api, settings, "remove", 3717)))
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={})
    assert r.status_code == 409 and r.json()["code"] == "concurrent_change"
    returned = next(s for s in r.json()["campaign"]["streams"] if s["id"] == OFFER_STREAM)
    fake.hooks.clear()
    st = current(api)
    assert st == returned
    assert st["dirty"] and next(l for l in st["links"] if l["offer_id"] == 3717)["status"] == "removed"


def test_push_claim_between_fetch_read_and_apply_wins(api, fake, settings):
    s = add(api, offer_stream(fetch(api)), 11112)

    def claim_push():
        with api.app.state.db.sessions() as session:
            row = session.get(Stream, OFFER_STREAM)
            row.push_state, row.draft_version = "pushing", row.draft_version + 1
            session.commit()

    original = fake.stream_offers(OFFER_STREAM)
    fake.hooks.append(("GET", rf"/campaigns/{CAMPAIGN}/streams", claim_push))
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": True})
    fake.hooks.clear()
    assert r.status_code == 409 and r.json()["code"] == "concurrent_change"
    st = current(api)
    assert st["dirty"] and st["push_state"] == "pushing"  # черновик не отброшен, захват Push не снят
    assert 11112 in [l["offer_id"] for l in st["links"] if l["status"] == "active"]
    # Фактическое состояние Keitaro: ни Fetch, ни конкурирующий захват ничего не записали.
    assert fake.stream_offers(OFFER_STREAM) == original
    assert puts(fake) == []
    # Захваченный поток остаётся заблокированным для Fetch и второго Push (без перехвата по возрасту).
    assert api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": True}).status_code == 423
    assert push(api, st).status_code == 423
    assert puts(fake) == []


def test_push_confirm_after_concurrent_resync_is_not_applied(api, fake, settings):
    s = add(api, offer_stream(fetch(api)), 11112)

    def resync_during_put():
        # Пока идёт PUT, локальное состояние «пересинхронизировано» в обход правил (онлайн-перехвата больше нет,
        # но CAS подтверждения должен устоять и против прямой правки базы).
        with api.app.state.db.sessions() as session:
            row = session.get(Stream, OFFER_STREAM)
            row.draft_version += 5
            row.push_state = "idle"
            session.commit()

    fake.hooks.append(("PUT", rf"/streams/{OFFER_STREAM}", resync_during_put))
    r = push(api, s)
    fake.hooks.clear()
    assert r.status_code == 409 and r.json()["code"] == "push_superseded"
    st = current(api)
    assert st["dirty"]  # подтверждение не применено к чужой версии
    # Фактически PUT применился ровно один раз: в Keitaro желаемый состав, а локально он честно «не подтверждён».
    assert fake.stream_offers(OFFER_STREAM) == [(3749, 25, "active"), (3717, 25, "active"), (11111, 25, "active"), (11112, 25, "active")]
    assert len(puts(fake)) == 1
    # Повтор Push сверяет и не пишет второй раз.
    r = push(api, st)
    assert r.status_code == 200 and r.json()["result"] == "already_applied", r.text
    assert len(puts(fake)) == 1


# --- 5. дубли связей ---

@pytest.mark.parametrize("dup", [
    {"offer_id": 3749, "share": 33, "state": "active"},    # одинаковый дубль
    {"offer_id": 3749, "share": 10, "state": "disabled"},  # различающийся дубль
])
def test_fetch_with_duplicate_links_is_explicit_error(api, fake, dup):
    c = fetch(api)
    before = offer_stream(c)
    fake.streams[OFFER_STREAM]["offers"].append(dict(dup, id=1, stream_id=OFFER_STREAM))
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={})
    assert r.status_code == 502 and r.json()["code"] == "keitaro_invalid_response"
    assert "3749" in r.json()["message"]
    assert current(api) == before


def test_duplicate_after_put_is_not_confirmed(api, fake):
    s = add(api, offer_stream(fetch(api)), 11112)
    fake.hooks.append(("PUT", rf"/streams/{OFFER_STREAM}", lambda: None))
    original = fake.handle

    def put_with_duplicate(method, path, params, body):
        status, data = original(method, path, params, body)
        if method == "PUT":
            fake.streams[OFFER_STREAM]["offers"].append({"id": 1, "offer_id": 11112, "share": 25, "state": "active"})
        return status, data

    fake.handle = put_with_duplicate
    r = push(api, s)
    assert r.status_code == 502 and r.json()["code"] == "push_unconfirmed"
    st = current(api)
    assert st["dirty"] and st["push_state"] == "idle"


# --- снимок защищённых полей перед PUT (повтор / already_applied / Fetch) ---

COUNTRY_RU = [{"name": "country", "mode": "reject", "payload": ["RU"]}]


def _fail_second_stream_get(fake):
    """Второе чтение потока (проверка после PUT) падает — результат Push неизвестен."""
    import httpx

    original = fake.mock_handler
    state = {"gets": 0}

    def flaky(request):
        if request.method == "GET" and request.url.path.endswith(f"/streams/{OFFER_STREAM}"):
            state["gets"] += 1
            if state["gets"] == 2:
                raise httpx.ConnectError("down", request=request)
        return original(request)

    fake.mock_handler = flaky
    return lambda: setattr(fake, "mock_handler", original)


def _baseline(api):
    with api.app.state.db.sessions() as session:
        return session.get(Stream, OFFER_STREAM).push_baseline


def _unconfirmed_destructive_push(api, fake):
    s = add(api, offer_stream(fetch(api)), 11112)
    filters_before = fake.streams[OFFER_STREAM]["filters"]
    fake.put_side_effects = {"filters": COUNTRY_RU}
    restore = _fail_second_stream_get(fake)
    r = push(api, s)
    restore()
    fake.put_side_effects = {}
    assert r.status_code == 502 and r.json()["code"] == "push_unconfirmed"
    baseline = _baseline(api)
    assert baseline is not None and baseline["filters"] == filters_before  # точный снимок до PUT в БД
    return current(api)


def test_destructive_put_then_readback_failure_then_retry_detects_side_effect(api, fake):
    s = _unconfirmed_destructive_push(api, fake)
    assert s["dirty"] and s["side_effect_fields"] == []
    r = push(api, s)  # офферы уже в Keitaro → already_applied, но сравнение идёт со снимком до первой записи
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "side_effect_detected" and body["changed_fields"] == ["filters"]
    assert body["stream"]["side_effect_fields"] == ["filters"] and not body["stream"]["dirty"]
    assert len(puts(fake)) == 1  # повторной записи не было
    assert _baseline(api) is None


def test_retry_with_drift_and_unapplied_offers_stops_without_second_put(api, fake):
    s = _unconfirmed_destructive_push(api, fake)
    fake.external_set_offers(OFFER_STREAM, [(3749, 33), (3717, 33), (11111, 34)])  # офферы «не применились»
    r = push(api, s)
    assert r.status_code == 409 and r.json()["code"] == "side_effect_detected"
    assert r.json()["changed_fields"] == ["filters"]
    assert len(puts(fake)) == 1
    st = current(api)
    assert st["side_effect_fields"] == ["filters"] and st["push_state"] == "idle"


def test_fetch_after_unconfirmed_destructive_push_reports_side_effect(api, fake):
    _unconfirmed_destructive_push(api, fake)
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": True})
    assert r.status_code == 200
    assert r.json()["report"]["side_effects"] == {str(OFFER_STREAM): ["filters"]}
    st = offer_stream(r.json()["campaign"])
    assert st["side_effect_fields"] == ["filters"]
    assert offer_stream(fetch(api))["side_effect_fields"] == []  # второй Fetch — явное подтверждение


def test_definitely_rejected_put_clears_baseline(api, fake):
    from app.devtools.fake_keitaro import Fault

    s = add(api, offer_stream(fetch(api)), 11112)
    fake.faults.append(Fault("PUT", rf"/streams/{OFFER_STREAM}", "error_400"))
    assert push(api, s).status_code == 422
    assert _baseline(api) is None
    r = push(api, current(api))
    assert r.json()["result"] == "published"


# --- неизвестный результат Push: правки и Cancel запрещены до сверки ---

def test_unknown_push_result_blocks_edits_until_reconciled(api, fake):
    s = add(api, offer_stream(fetch(api)), 11112)
    fake.put_side_effects = {"filters": COUNTRY_RU}           # PUT применился и изменил защищённое поле
    restore = _fail_second_stream_get(fake)                   # проверочный GET упал
    r = push(api, s)
    restore()
    fake.put_side_effects = {}
    assert r.status_code == 502 and r.json()["code"] == "push_unconfirmed"

    st = current(api)
    assert st["dirty"] and st["reconciliation_required"] is True
    assert "push_baseline" not in st and "filters" not in str(st.get("reconciliation_required"))  # снимок не отдаётся
    warning = st["last_error"]
    assert warning and "неизвестен" in warning

    v = st["draft_version"]
    attempts = [
        ("/offers", {"offer_id": 24800}),
        ("/offers/3749/remove", {}),
        ("/offers/3749/pin", {"share": 30}),
        ("/offers/3749/unpin", {}),
        ("/cancel", {}),
    ]
    for path, extra in attempts:
        r = api.post(f"/api/streams/{OFFER_STREAM}{path}", json={"expected_version": v, **extra})
        assert r.status_code == 409 and r.json()["code"] == "reconciliation_required", (path, r.text)
        assert r.json()["stream"]["reconciliation_required"] is True

    st = current(api)
    assert st["dirty"] and st["reconciliation_required"] and st["draft_version"] == v
    assert st["last_error"] == warning  # предупреждение не очищено
    assert [o for o, _, _ in fake.stream_offers(OFFER_STREAM)] == [3749, 3717, 11111, 11112]

    r = push(api, st)  # безопасный повтор доступен и выявляет побочный эффект
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "side_effect_detected" and r.json()["changed_fields"] == ["filters"]
    assert r.json()["stream"]["reconciliation_required"] is False
    assert len(puts(fake)) == 1  # PUT выполнен ровно один раз


def test_bring_back_is_blocked_while_reconciliation_required(api, fake):
    s = offer_stream(fetch(api))
    s = api.post(f"/api/streams/{OFFER_STREAM}/offers/3717/remove", json={"expected_version": s["draft_version"]}).json()
    restore = _fail_second_stream_get(fake)
    assert push(api, s).status_code == 502
    restore()
    st = current(api)
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers/3717/restore", json={"expected_version": st["draft_version"]})
    assert r.status_code == 409 and r.json()["code"] == "reconciliation_required"
    # Fetch тоже доступен и снимает флаг
    st = offer_stream(fetch(api, discard=True))
    assert st["reconciliation_required"] is False and st["last_error"] is None


def test_regular_stream_reports_no_reconciliation(api):
    assert offer_stream(fetch(api))["reconciliation_required"] is False


# --- campaign_id изменён неподтверждённым PUT ---

def _unconfirmed_push_moving_stream(api, fake):
    s = add(api, offer_stream(fetch(api)), 11112)
    fake.put_side_effects = {"campaign_id": 7777}  # PUT применился и «перенёс» поток в другую кампанию
    restore = _fail_second_stream_get(fake)
    r = push(api, s)
    restore()
    fake.put_side_effects = {}
    assert r.status_code == 502 and r.json()["code"] == "push_unconfirmed"
    st = current(api)
    assert st["reconciliation_required"] is True and st["dirty"]
    return st


def test_campaign_id_change_resolved_by_retry_push(api, fake):
    st = _unconfirmed_push_moving_stream(api, fake)
    calls_before = len(fake.calls)
    r = push(api, st)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "side_effect_detected" and body["changed_fields"] == ["campaign_id"]
    assert body["stream"]["reconciliation_required"] is False
    assert body["stream"]["side_effect_fields"] == ["campaign_id"]
    retry_calls = fake.calls[calls_before:]
    assert retry_calls == [("GET", f"/streams/{OFFER_STREAM}")]  # сверка до чтения кампании и без записи
    assert len(puts(fake)) == 1


def test_campaign_id_change_resolved_by_fetch(api, fake):
    _unconfirmed_push_moving_stream(api, fake)
    calls_before = len(fake.calls)
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": True})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["side_effects"] == {str(OFFER_STREAM): ["campaign_id"]}
    st = next(x for x in r.json()["campaign"]["streams"] if x["id"] == OFFER_STREAM)
    assert st["reconciliation_required"] is False and st["remote_missing"] is True
    assert st["side_effect_fields"] == ["campaign_id"] and "не входит в кампанию" in st["last_error"]
    assert ("GET", f"/streams/{OFFER_STREAM}") in fake.calls[calls_before:]  # поток прочитан отдельно по ID
    assert len(puts(fake)) == 1
    r = push(api, st)  # поток вне кампании с зафиксированным побочным эффектом — публикация остановлена
    assert r.status_code == 409 and r.json()["code"] == "side_effect_unresolved"
    assert len(puts(fake)) == 1


def test_fetch_completes_reconciliation_for_deleted_stream(api, fake):
    _unconfirmed_destructive_push(api, fake)
    del fake.streams[OFFER_STREAM]
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": True})
    assert r.status_code == 200, r.text
    st = next(x for x in r.json()["campaign"]["streams"] if x["id"] == OFFER_STREAM)
    assert st["remote_missing"] is True and st["reconciliation_required"] is False
    assert st["side_effect_fields"] == [] and "удалён" in st["last_error"]
    assert len(puts(fake)) == 1


def test_blocked_add_does_not_call_keitaro(api, fake):
    st = _unconfirmed_destructive_push(api, fake)
    calls_before = len(fake.calls)
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers", json={"expected_version": st["draft_version"], "offer_id": 24800})
    assert r.status_code == 409 and r.json()["code"] == "reconciliation_required"
    assert fake.calls[calls_before:] == []


def test_add_rechecks_reconciliation_after_offer_lookup(api, fake, settings):
    """Снимок появился, пока Add читал оффер в Keitaro: повторная проверка после CAS всё равно блокирует."""
    st = offer_stream(fetch(api))

    def baseline_appears():
        with api.app.state.db.sessions() as session:
            session.get(Stream, OFFER_STREAM).push_baseline = {"name": "x"}
            session.commit()

    fake.hooks.append(("GET", r"/offers/24800", baseline_appears))
    r = api.post(f"/api/streams/{OFFER_STREAM}/offers", json={"expected_version": st["draft_version"], "offer_id": 24800})
    fake.hooks.clear()
    assert r.status_code == 409 and r.json()["code"] == "reconciliation_required"
    assert 24800 not in [l["offer_id"] for l in current(api)["links"]]
