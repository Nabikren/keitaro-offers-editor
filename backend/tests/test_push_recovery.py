"""Зависший Push: никакого онлайн-перехвата по возрасту; восстановление — только офлайн и только чтением."""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.devtools.fake_keitaro import API_KEY, FakeKeitaro
from app.keitaro.client import KeitaroClient
from app.main import create_app
from app.models import Stream
from app.recover import ReadOnlyKeitaro, run as run_recovery
from app.runlock import LockBusy
from app.services.editor import EditorService
from app.services.errors import ServiceError

CAMPAIGN, OFFER_STREAM = 5000, 5002
DESIRED_4 = [(3749, 25, "active"), (3717, 25, "active"), (11111, 25, "active"), (11112, 25, "active")]
ORIGINAL_3 = [(3749, 33, "active"), (3717, 33, "active"), (11111, 34, "active")]


def puts(fake):
    return [p for m, p in fake.calls if m == "PUT"]


def build(tmp_path, fake):
    settings = Settings(_env_file=None, keitaro_url="https://kt.test", keitaro_api_key=API_KEY,
                        database_url=f"sqlite:///{tmp_path / f'{uuid.uuid4().hex}.sqlite3'}",
                        campaign_name_prefix="test_simonov_", push_stale_seconds=60)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    client = lambda: KeitaroClient(settings.keitaro_url, API_KEY, 2, transport=fake.transport())  # noqa: E731
    return create_app(settings, kt_factory=client), settings, client


def offer_stream(api):
    return next(s for s in api.get(f"/api/campaigns/{CAMPAIGN}").json()["streams"] if s["id"] == OFFER_STREAM)


def fetch(api, discard=False):
    r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": discard})
    assert r.status_code == 200, r.text
    return next(s for s in r.json()["campaign"]["streams"] if s["id"] == OFFER_STREAM)


def add(api, s, offer_id):
    r = api.post(f"/api/streams/{s['id']}/offers", json={"expected_version": s["draft_version"], "offer_id": offer_id})
    assert r.status_code == 200, r.text
    return r.json()


def push(api, s):
    return api.post(f"/api/streams/{s['id']}/push", json={"expected_version": s["draft_version"]})


def set_row(app, **values):
    with app.state.db.sessions() as session:
        row = session.get(Stream, OFFER_STREAM)
        for k, v in values.items():
            setattr(row, k, v)
        session.commit()


def row(app) -> Stream:
    with app.state.db.sessions() as session:
        return session.get(Stream, OFFER_STREAM)


@pytest.fixture
def fake():
    return FakeKeitaro().seed_demo()


# --- 1. просроченный pushing ничем не перехватывается ---

def test_expired_pushing_blocks_fetch_and_second_push(tmp_path, fake):
    app, _, _ = build(tmp_path, fake)
    with TestClient(app) as api:
        s = add(api, fetch(api), 11112)
        set_row(app, push_state="pushing", push_started_at=datetime.now(timezone.utc) - timedelta(hours=3))
        st = offer_stream(api)
        assert st["push_state"] == "pushing" and st["push_stale"] is True

        r = api.post(f"/api/campaigns/{CAMPAIGN}/fetch", json={"discard_drafts": True})
        assert r.status_code == 423 and r.json()["code"] == "push_in_progress"
        assert r.json()["stale_streams"] == [OFFER_STREAM] and "app.recover" in r.json()["message"]

        for version in (s["draft_version"], st["draft_version"], st["draft_version"] + 1):
            r = api.post(f"/api/streams/{OFFER_STREAM}/push", json={"expected_version": version})
            assert r.status_code == 423 and r.json()["code"] == "push_in_progress"

        r = api.post(f"/api/streams/{OFFER_STREAM}/offers/3749/remove", json={"expected_version": st["draft_version"]})
        assert r.status_code == 423

        assert puts(fake) == []
        assert fake.stream_offers(OFFER_STREAM) == ORIGINAL_3
        assert row(app).push_state == "pushing"  # ничего не «снято» онлайн


# --- 2. старение во время PUT не даёт перехвата ---

def test_aging_during_put_does_not_allow_takeover(tmp_path, fake):
    app, settings, client = build(tmp_path, fake)
    with TestClient(app) as api:
        s = add(api, fetch(api), 11112)
        seen = {}

        def age_and_interfere():
            set_row(app, push_started_at=datetime.now(timezone.utc) - timedelta(hours=1))
            with app.state.db.sessions() as session:
                svc = EditorService(session, client(), settings)
                for name, call in (("fetch", lambda: svc.fetch(CAMPAIGN, discard_drafts=True)),
                                   ("push", lambda: svc.push(OFFER_STREAM, session.get(Stream, OFFER_STREAM).draft_version))):
                    try:
                        call()
                        seen[name] = "ok"
                    except ServiceError as e:
                        seen[name] = (e.status, e.code)

        fake.hooks.append(("PUT", rf"/streams/{OFFER_STREAM}", age_and_interfere))
        r = push(api, s)
        fake.hooks.clear()

        assert seen == {"fetch": (423, "push_in_progress"), "push": (423, "push_in_progress")}
        assert r.status_code == 200 and r.json()["result"] == "published", r.text
        st = r.json()["stream"]
        assert not st["dirty"] and st["push_state"] == "idle" and st["reconciliation_required"] is False
        assert fake.stream_offers(OFFER_STREAM) == DESIRED_4
        assert len(puts(fake)) == 1


# --- 3. офлайн-сверка: только чтение ---

COUNTRY_RU = [{"name": "country", "mode": "reject", "payload": ["RU"]}]


def _crash_after_put(app, api, fake, side_effects):
    """PUT применился, проверочное чтение не удалось; затем процесс «упал» посреди следующего Push."""
    s = add(api, fetch(api), 11112)
    fake.put_side_effects = side_effects
    original, state = fake.mock_handler, {"gets": 0}

    def flaky(request):
        if request.method == "GET" and request.url.path.endswith(f"/streams/{OFFER_STREAM}"):
            state["gets"] += 1
            if state["gets"] == 2:
                raise httpx.ConnectError("down", request=request)
        return original(request)

    fake.mock_handler = flaky
    assert push(api, s).status_code == 502
    fake.mock_handler, fake.put_side_effects = original, {}
    set_row(app, push_state="pushing", push_started_at=datetime.now(timezone.utc) - timedelta(hours=2))
    assert row(app).push_baseline is not None


def test_offline_recovery_resolves_baseline_by_read_only(tmp_path, fake):
    app, settings, client = build(tmp_path, fake)
    with TestClient(app) as api:
        _crash_after_put(app, api, fake, {"filters": COUNTRY_RU})
        with pytest.raises(LockBusy):  # пока backend работает, офлайн-сверка не запускается
            run_recovery(settings, ReadOnlyKeitaro(client()))
    # backend остановлен
    calls_before = len(fake.calls)
    report = run_recovery(settings, ReadOnlyKeitaro(client()))
    assert [(i["stream_id"], i["outcome"], i["side_effect_fields"]) for i in report] == [(OFFER_STREAM, "applied", ["filters"])]
    assert fake.calls[calls_before:] == [("GET", f"/streams/{OFFER_STREAM}")]  # только чтение
    assert len(puts(fake)) == 1  # единственный PUT — исходный

    r = row(app)
    assert r.push_state == "idle" and r.push_baseline is None and r.side_effect_fields == ["filters"]
    assert "Изменились защищённые поля: filters" in r.last_error
    with TestClient(app) as api:  # backend снова стартует
        st = offer_stream(api)
        assert not st["dirty"] and st["reconciliation_required"] is False and st["side_effect_fields"] == ["filters"]
        assert push(api, st).json()["code"] == "side_effect_unresolved"
    assert len(puts(fake)) == 1


def test_offline_recovery_when_write_did_not_apply(tmp_path, fake):
    app, settings, client = build(tmp_path, fake)
    with TestClient(app) as api:
        _crash_after_put(app, api, fake, {})
    fake.external_set_offers(OFFER_STREAM, [(o, sh) for o, sh, _ in ORIGINAL_3])  # «запись не применилась»
    report = run_recovery(settings, ReadOnlyKeitaro(client()))
    assert [(i["outcome"], i["side_effect_fields"]) for i in report] == [("not_applied", [])]
    with TestClient(app) as api:
        st = offer_stream(api)
        assert st["dirty"] and st["push_state"] == "idle" and st["reconciliation_required"] is False
        r = push(api, st)  # теперь повтор Push безопасен и пишет ровно один раз
        assert r.status_code == 200 and r.json()["result"] == "published"
    assert fake.stream_offers(OFFER_STREAM) == DESIRED_4
    assert len(puts(fake)) == 2  # исходный (не применился) + повтор после сверки


def test_offline_recovery_for_deleted_stream(tmp_path, fake):
    app, settings, client = build(tmp_path, fake)
    with TestClient(app) as api:
        _crash_after_put(app, api, fake, {})
    del fake.streams[OFFER_STREAM]
    report = run_recovery(settings, ReadOnlyKeitaro(client()))
    assert [i["outcome"] for i in report] == ["missing"]
    r = row(app)
    assert r.remote_missing and r.push_state == "idle" and r.push_baseline is None
    assert len(puts(fake)) == 1


def test_offline_recovery_keeps_state_on_read_error(tmp_path, fake):
    app, settings, client = build(tmp_path, fake)
    with TestClient(app) as api:
        _crash_after_put(app, api, fake, {})
    from app.devtools.fake_keitaro import Fault
    fake.faults.append(Fault("GET", rf"/streams/{OFFER_STREAM}", "error_500_before"))
    report = run_recovery(settings, ReadOnlyKeitaro(client()))
    assert [i["outcome"] for i in report] == ["error"]
    r = row(app)
    assert r.push_state == "pushing" and r.push_baseline is not None  # ничего не угадываем
    assert len(puts(fake)) == 1


def test_backend_does_not_start_while_recovery_holds_lock(tmp_path, fake):
    from app.runlock import RunLock, lock_path_for

    app, settings, _ = build(tmp_path, fake)
    lock = RunLock(lock_path_for(settings.database_url))
    lock.acquire("Офлайн-сверка")
    try:
        with pytest.raises(LockBusy):
            with TestClient(app):
                pass
    finally:
        lock.release()
    with TestClient(app) as api:
        assert api.get("/api/health").status_code == 200


def test_read_only_client_cannot_write():
    kt = ReadOnlyKeitaro(KeitaroClient("https://kt.test", API_KEY, 2, transport=FakeKeitaro().transport()))
    assert not hasattr(kt, "update_stream_offers") and not hasattr(kt, "create_stream") and not hasattr(kt, "create_campaign")
