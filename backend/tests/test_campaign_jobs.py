"""Hardening создания кампании: аренда задания, восстановление после падения, некорректные ответы,
конкурентный resume, строгая проверка результата и конфликты сверки."""

import threading
import time
import uuid

import pytest

from app.devtools.fake_keitaro import Fault
from app.keitaro import payloads
from app.keitaro.client import KeitaroClient
from app.models import CampaignJob
from app.services.campaigns import CampaignCreator


def body(**over):
    data = {"idempotency_key": uuid.uuid4().hex, "name": "job-test", "geo": "AU", "offer_id": 10184,
            "domain_id": 1813, "group_id": 10332, "traffic_source_id": 836}
    data.update(over)
    return data


def posts(fake, path):
    return [p for m, p in fake.calls if m == "POST" and p == path]


def our_streams(fake, campaign_id):
    return [s for s in fake.streams.values() if s["campaign_id"] == campaign_id]


def set_job(api, key, **values):
    with api.app.state.db.sessions() as session:
        job = session.query(CampaignJob).filter_by(idempotency_key=key).one()
        for k, v in values.items():
            setattr(job, k, v)
        session.commit()


def get_job(api, key):
    with api.app.state.db.sessions() as session:
        return session.query(CampaignJob).filter_by(idempotency_key=key).one()


# --- падение процесса и зависшее running ---

def test_crash_after_stream_post_does_not_leave_job_running(api, fake, monkeypatch):
    calls = {"n": 0}
    real = KeitaroClient.create_stream

    def crash_after_second(self, payload):
        result = real(self, payload)
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("process died right after POST /streams")
        return result

    monkeypatch.setattr(KeitaroClient, "create_stream", crash_after_second)
    data = body()
    job = api.post("/api/campaigns", json=data).json()
    assert job["status"] == "partial" and "Внутренняя ошибка" in job["last_error"]
    assert get_job(api, data["idempotency_key"]).lease_token is None
    monkeypatch.setattr(KeitaroClient, "create_stream", real)
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "done"
    assert len(posts(fake, "/streams")) == 2  # поток оффера найден сверкой, а не создан повторно
    assert len(our_streams(fake, resumed["campaign_id"])) == 2


def test_stale_running_job_is_reconciled_by_alias_without_new_post(api, fake):
    data = body()
    fake.faults.append(Fault("POST", "/campaigns", "error_400"))
    job = api.post("/api/campaigns", json=data).json()
    assert job["status"] == "failed"
    # Имитация: процесс упал сразу после того, как Keitaro создал кампанию; задание осталось running.
    fake._create_campaign(payloads.campaign_payload(name=job["name"], alias=job["alias"], group_id=10332, domain_id=1813, traffic_source_id=836))
    set_job(api, data["idempotency_key"], status="running", lease_token="dead", lease_until=int(time.time()) - 5)
    before = len(posts(fake, "/campaigns"))
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "done"
    assert len(posts(fake, "/campaigns")) == before  # без повторного POST /campaigns
    assert len([c for c in fake.campaigns.values() if c["alias"] == job["alias"]]) == 1


def test_running_job_with_valid_lease_is_not_resumed(api, fake):
    data = body()
    fake.faults.append(Fault("POST", "/campaigns", "error_400"))
    api.post("/api/campaigns", json=data)
    set_job(api, data["idempotency_key"], status="running", lease_token="alive", lease_until=int(time.time()) + 60)
    writes_before = [c for c in fake.calls if c[0] != "GET"]
    r = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume")
    assert r.status_code == 409 and r.json()["code"] == "job_running"
    assert r.json()["job"]["stale"] is False
    assert [c for c in fake.calls if c[0] != "GET"] == writes_before
    # повторный start с тем же ключом тоже ничего не выполняет
    assert api.post("/api/campaigns", json=data).json()["status"] == "running"


# --- некорректные ответы ---

@pytest.mark.parametrize("path", ["/campaigns", "/streams"])
def test_success_without_id_after_apply_is_reconciled(api, fake, path):
    fake.faults.append(Fault("POST", path, "malformed_after"))
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] == "done", job
    assert len(posts(fake, path)) == (1 if path == "/campaigns" else 2)
    assert len(our_streams(fake, job["campaign_id"])) == 2


@pytest.mark.parametrize("path", ["/campaigns", "/streams"])
def test_success_without_id_not_applied_is_controlled_partial(api, fake, path):
    data = body()
    fake.faults.append(Fault("POST", path, "malformed_before"))
    job = api.post("/api/campaigns", json=data).json()
    assert job["status"] in ("partial", "failed") and "не подтверждено" in job["last_error"]
    assert get_job(api, data["idempotency_key"]).status != "running"
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "done"
    assert len([c for c in fake.campaigns.values() if c["alias"] == resumed["alias"]]) == 1
    assert len(our_streams(fake, resumed["campaign_id"])) == 2


def test_invalid_json_on_post_is_controlled(api, fake):
    data = body()
    fake.faults.append(Fault("POST", "/streams", "invalid_json"))
    job = api.post("/api/campaigns", json=data).json()
    assert job["status"] == "partial" and job["last_error"]
    assert api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()["status"] == "done"


# --- конкурентный resume ---

def test_concurrent_resume_executes_posts_once(api, fake, settings):
    data = body()
    fake.faults.append(Fault("POST", "/campaigns", "error_400"))
    assert api.post("/api/campaigns", json=data).json()["status"] == "failed"

    entered, release = threading.Event(), threading.Event()

    def slow_post():
        entered.set()
        assert release.wait(5)

    fake.hooks.append(("POST", "/campaigns", slow_post))
    results = {}

    def resume(name):
        with api.app.state.db.sessions() as session:
            kt = KeitaroClient(settings.keitaro_url, settings.keitaro_api_key, 10, transport=fake.transport())
            try:
                results[name] = CampaignCreator(session, kt, settings).resume(data["idempotency_key"])
            except Exception as e:  # noqa: BLE001
                results[name] = e

    a = threading.Thread(target=resume, args=("a",))
    a.start()
    assert entered.wait(5)
    resume("b")  # пока A держит аренду и стоит внутри POST
    release.set()
    a.join(10)
    assert getattr(results["b"], "code", None) == "job_running"
    assert results["a"]["status"] == "done"
    assert len(posts(fake, "/campaigns")) == 2  # 1 неудачная первая попытка (400) + 1 у A; у B — ни одного


# --- строгая проверка и сверка ---

@pytest.mark.parametrize("field, value", [("type", None), ("type", "weight"), ("domain_id", None), ("domain_id", 1),
                                          ("group_id", None), ("traffic_source_id", 999), ("alias", "other")])
def test_campaign_verification_is_strict(api, fake, field, value):
    original = fake.handle

    def patched(method, path, params, b):
        status, data = original(method, path, params, b)
        if method == "GET" and path.startswith("/campaigns/") and path.count("/") == 2 and isinstance(data, dict):
            data = dict(data)
            if value is None:
                data.pop(field, None)
            else:
                data[field] = value
        return status, data

    fake.handle = patched
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] == "partial" and f"кампания.{field}" in job["last_error"]


@pytest.mark.parametrize("field, value", [("type", "default"), ("state", "disabled"), ("schema", "landings"), ("action_type", "location"),
                                          ("action_payload", "https://google.com"), ("action_payload", "https://www.google.com/search"),
                                          ("action_payload", "http://www.google.com/"), ("campaign_id", 4242),
                                          ("position", 3), ("name", "renamed"),
                                          ("filters", [{"name": "country", "mode": "reject", "payload": ["AU"]}])])
def test_geo_stream_shape_is_verified(api, fake, field, value):
    original = fake.handle

    def patched(method, path, params, b):
        status, data = original(method, path, params, b)
        if method == "GET" and path.endswith("/streams") and isinstance(data, list):
            data = [dict(s, **{field: value}) if s.get("position") == 1 else s for s in data]
        return status, data

    fake.handle = patched
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] != "done"
    assert field in job["last_error"]


def test_offer_stream_offers_are_verified(api, fake):
    original = fake.handle

    def patched(method, path, params, b):
        status, data = original(method, path, params, b)
        if method == "GET" and path.endswith("/streams") and isinstance(data, list):
            data = [dict(s, offers=[{"offer_id": 10184, "share": 50, "state": "active"}]) if s.get("position") == 2 else s for s in data]
        return status, data

    fake.handle = patched
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] == "partial" and "offers" in job["last_error"]


def _prepare_campaign_for_resume(api, fake):
    data = body()
    fake.faults.append(Fault("POST", "/streams", "error_400"))
    job = api.post("/api/campaigns", json=data).json()
    assert job["status"] == "partial" and job["campaign_id"] and job["geo_stream_id"] is None
    return data, job


def test_reconcile_rejects_candidate_with_different_shape(api, fake):
    data, job = _prepare_campaign_for_resume(api, fake)
    wrong = payloads.geo_stream_payload(campaign_id=job["campaign_id"], countries=["RU"], redirect_url="https://www.google.com/")
    fake._create_stream(wrong)
    n = len(posts(fake, "/streams"))
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "partial" and "не совпадает" in resumed["last_error"]
    assert len(posts(fake, "/streams")) == n  # ничего не создано поверх


def test_reconcile_with_two_candidates_is_conflict(api, fake):
    data, job = _prepare_campaign_for_resume(api, fake)
    right = payloads.geo_stream_payload(campaign_id=job["campaign_id"], countries=["AU"], redirect_url="https://www.google.com/")
    fake._create_stream(right)
    fake._create_stream(right)
    n = len(posts(fake, "/streams"))
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "partial" and "Несколько потоков" in resumed["last_error"]
    assert len(posts(fake, "/streams")) == n


def test_reconcile_rejects_campaign_with_same_alias_but_other_name(api, fake):
    data = body()
    fake.faults.append(Fault("POST", "/campaigns", "error_400"))
    job = api.post("/api/campaigns", json=data).json()
    fake._create_campaign({"alias": job["alias"], "name": "test_simonov_other", "group_id": 10332})
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "failed" and "кампания.name" in resumed["last_error"]
    assert len(posts(fake, "/campaigns")) == 1


# --- конфигурация Keitaro до аренды ---

def test_resume_without_keitaro_config_does_not_claim_job(api, fake, settings):
    from app.services.errors import ServiceError

    data = body()
    fake.faults.append(Fault("POST", "/streams", "error_400"))
    assert api.post("/api/campaigns", json=data).json()["status"] == "partial"
    with api.app.state.db.sessions() as session:
        with pytest.raises(ServiceError) as e:
            CampaignCreator(session, None, settings).resume(data["idempotency_key"])
    assert e.value.status == 503 and e.value.code == "keitaro_not_configured"
    job = get_job(api, data["idempotency_key"])
    assert job.status == "partial" and job.lease_token is None and job.lease_until is None


def test_start_without_keitaro_config_creates_no_job(api, settings):
    from app.services.errors import ServiceError

    data = body()
    with api.app.state.db.sessions() as session:
        with pytest.raises(ServiceError) as e:
            CampaignCreator(session, None, settings).start(data["idempotency_key"], data)
        assert e.value.code == "keitaro_not_configured"
        assert session.query(CampaignJob).count() == 0


# --- нормализация redirect URL ---

@pytest.mark.parametrize("a, b, equal", [
    ("https://www.google.com/", "https://www.google.com", True),
    ("https://www.google.com/", "HTTPS://WWW.Google.COM/", True),
    ("https://www.google.com/", "https://www.google.com:443/", True),
    ("http://example.com/path/", "http://example.com/path", True),
    ("https://www.google.com/", "http://www.google.com/", False),
    ("https://www.google.com/", "https://www.google.com/?q=1", False),
    ("https://www.google.com/", "https://www.google.com:8443/", False),
    ("https://www.google.com/", "https://google.com/", False),
    ("https://[::1]:8443/", "https://[::1:8443]/", False),
    ("https://[::1]:443/", "https://[::1]", True),
    ("https://[::1]/", "HTTPS://[::1]", True),
    ("https://[2001:db8::1]:8080/a/", "https://[2001:DB8::1]:8080/a", True),
    ("https://www.goo gle.com/", "https://www.goo gle.com/", True),   # строка равна самой себе,
    ("https://www.goo gle.com/", "https://www.google.com/", False),   # но не корректному URL
])


def test_redirect_url_normalization(a, b, equal):
    from app.services.campaigns import normalize_http_url
    assert (normalize_http_url(a) == normalize_http_url(b)) is equal


def test_ipv6_normalization_keeps_brackets():
    from app.services.campaigns import normalize_http_url
    assert normalize_http_url("https://[::1]:8443/") == "https://[::1]:8443"
    assert normalize_http_url("https://[::1:8443]/") == "https://[::1:8443]"


@pytest.mark.parametrize("returned", ["https://www.goo gle.com/", "https://www.goo\tgle.com/"])
def test_redirect_with_whitespace_from_keitaro_is_rejected(api, fake, returned):
    original = fake.handle

    def patched(method, path, params, b):
        status, data = original(method, path, params, b)
        if method == "GET" and path.endswith("/streams") and isinstance(data, list):
            data = [dict(s, action_payload=returned) if s.get("position") == 1 else s for s in data]
        return status, data

    fake.handle = patched
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] == "partial" and "action_payload" in job["last_error"]



@pytest.mark.parametrize("returned", ["https://www.google.com", "HTTPS://WWW.GOOGLE.COM/", "https://www.google.com:443"])
def test_equivalent_redirect_url_from_keitaro_is_accepted(api, fake, returned):
    original = fake.handle

    def patched(method, path, params, b):
        status, data = original(method, path, params, b)
        if method == "GET" and path.endswith("/streams") and isinstance(data, list):
            data = [dict(s, action_payload=returned) if s.get("position") == 1 else s for s in data]
        return status, data

    fake.handle = patched
    assert api.post("/api/campaigns", json=body()).json()["status"] == "done"


def test_redirect_url_setting_must_be_http():
    from pydantic import ValidationError

    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(_env_file=None, redirect_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, redirect_url="www.google.com")


@pytest.mark.parametrize("url", ["https://host:bad", "https://host:99999", "https://host:-1/", "http://[::1",
                                 "https://www.goo gle.com/", "https://goo\tgle.com/", "https://google.com/\x00",
                                 "https://goo\ngle.com/", "https://google.com\x7f/"])
def test_redirect_url_with_invalid_port_is_rejected_by_validator(url):
    from pydantic import ValidationError

    from app.config import Settings
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, redirect_url=url)
    assert "REDIRECT_URL" in str(e.value)


def test_redirect_url_with_valid_port_is_accepted():
    from app.config import Settings
    assert Settings(_env_file=None, redirect_url="https://host:8443/").redirect_url == "https://host:8443/"


# --- сверка кампании до POST /streams ---

@pytest.mark.parametrize("field, value", [("type", "weight"), ("name", "test_simonov_renamed"), ("alias", "other"),
                                          ("domain_id", 1), ("group_id", 9799), ("traffic_source_id", 7)])
def test_resume_checks_campaign_before_creating_streams(api, fake, field, value):
    data, job = _prepare_campaign_for_resume(api, fake)
    fake.campaigns[job["campaign_id"]][field] = value
    n = len(posts(fake, "/streams"))
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "partial" and f"кампания.{field}" in resumed["last_error"]
    assert len(posts(fake, "/streams")) == n  # ни одного POST /streams


def test_reconciled_campaign_by_alias_is_fully_checked_before_streams(api, fake):
    data = body()
    fake.faults.append(Fault("POST", "/campaigns", "error_400"))
    job = api.post("/api/campaigns", json=data).json()
    fake._create_campaign(payloads.campaign_payload(name=job["name"], alias=job["alias"], group_id=10332,
                                                    domain_id=1813, traffic_source_id=999))
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "failed" and "кампания.traffic_source_id" in resumed["last_error"]
    assert posts(fake, "/streams") == []


def test_reconcile_rejects_stream_candidate_with_other_campaign_id(api, fake):
    data, job = _prepare_campaign_for_resume(api, fake)
    right = payloads.geo_stream_payload(campaign_id=job["campaign_id"], countries=["AU"], redirect_url="https://www.google.com/")
    created = fake._create_stream(right)
    created["campaign_id"] = 4242  # Keitaro вернёт поток в списке кампании, но с чужим campaign_id
    original = fake.handle

    def listed_anyway(method, path, params, b):
        status, data_ = original(method, path, params, b)
        if method == "GET" and path == f"/campaigns/{job['campaign_id']}/streams":
            data_ = data_ + [created]
        return status, data_

    fake.handle = listed_anyway
    n = len(posts(fake, "/streams"))
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "partial" and "campaign_id" in resumed["last_error"]
    assert len(posts(fake, "/streams")) == n
