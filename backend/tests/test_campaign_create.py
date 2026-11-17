"""Часть A: создание кампании с двумя потоками, идемпотентность и сверка при неоднозначных ответах."""

import uuid

import httpx
import pytest

from app.devtools.fake_keitaro import Fault
from app.services.campaigns import parse_geo
from app.services.errors import ServiceError


def body(**over):
    data = {"idempotency_key": uuid.uuid4().hex, "name": "mx-landing", "geo": "MX", "offer_id": 10184,
            "domain_id": 1813, "group_id": 9799, "traffic_source_id": 836}
    data.update(over)
    return data


def writes(fake):
    return [(m, p) for m, p in fake.calls if m != "GET"]


def test_creates_campaign_with_two_ordered_streams(api, fake):
    r = api.post("/api/campaigns", json=body())
    job = r.json()
    assert r.status_code == 200 and job["status"] == "done", job
    camp = fake.campaigns[job["campaign_id"]]
    assert camp["name"] == "test_simonov_mx-landing" and camp["type"] == "position"
    assert (camp["domain_id"], camp["group_id"], camp["traffic_source_id"]) == (1813, 9799, 836)
    geo, off = fake.streams[job["geo_stream_id"]], fake.streams[job["offer_stream_id"]]
    assert geo["position"] < off["position"]
    assert (geo["schema"], geo["action_type"], geo["action_payload"]) == ("redirect", "http", "https://www.google.com/")
    assert [(f["name"], f["mode"], f["payload"]) for f in geo["filters"]] == [("country", "accept", ["MX"])]
    assert geo["offers"] == []
    assert off["filters"] == [] and fake.stream_offers(off["id"]) == [(10184, 100, "active")]
    assert writes(fake) == [("POST", "/campaigns"), ("POST", "/streams"), ("POST", "/streams")]


def test_double_submit_with_same_key_creates_once(api, fake):
    data = body()
    first = api.post("/api/campaigns", json=data).json()
    second = api.post("/api/campaigns", json=data).json()
    assert first["campaign_id"] == second["campaign_id"]
    assert len([c for c in fake.campaigns.values() if c["name"].endswith("mx-landing")]) == 1


def test_multiple_countries_extension(api, fake):
    job = api.post("/api/campaigns", json=body(geo="mx, au;RO")).json()
    assert fake.streams[job["geo_stream_id"]]["filters"][0]["payload"] == ["MX", "AU", "RO"]


def test_prefix_is_not_duplicated(api, fake):
    job = api.post("/api/campaigns", json=body(name="test_simonov_x1")).json()
    assert fake.campaigns[job["campaign_id"]]["name"] == "test_simonov_x1"


@pytest.mark.parametrize("over, code", [
    ({"geo": ""}, "geo_required"),
    ({"geo": "UK"}, "geo_invalid"),
    ({"geo": "MX,ZZ"}, "geo_invalid"),
    ({"offer_id": 424242}, "offer_not_found"),
    ({"offer_id": 999}, "offer_unavailable"),
    ({"offer_id": 0}, "invalid_offer_id"),
    ({"domain_id": 1}, "domain_id_invalid"),
    ({"group_id": None}, "group_id_invalid"),
    ({"traffic_source_id": 77}, "traffic_source_id_invalid"),
    ({"name": "   "}, "name_invalid"),
    ({"name": "bad/name"}, "name_invalid"),
])
def test_validation_errors_make_no_writes(api, fake, over, code):
    r = api.post("/api/campaigns", json=body(**over))
    assert r.status_code == 422 and r.json()["code"] == code, r.text
    assert writes(fake) == []


def test_lost_campaign_response_is_reconciled_by_alias(api, fake):
    fake.faults.append(Fault("POST", "/campaigns", "timeout_after"))
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] == "done"
    assert len([w for w in writes(fake) if w[1] == "/campaigns"]) == 1
    assert fake.campaigns[job["campaign_id"]]["alias"] == job["alias"]


def test_lost_stream_response_is_reconciled_without_duplicate(api, fake):
    fake.faults.append(Fault("POST", "/streams", "error_500_after"))
    job = api.post("/api/campaigns", json=body()).json()
    assert job["status"] == "done"
    streams = [s for s in fake.streams.values() if s["campaign_id"] == job["campaign_id"]]
    assert len(streams) == 2


def test_partial_result_is_kept_and_resume_continues(api, fake):
    # 1-й поток создан, создание 2-го точно не применилось (connect timeout)
    data = body()
    original = fake.mock_handler
    count = {"streams": 0}

    def second_stream_fails(request):
        if request.method == "POST" and request.url.path.endswith("/streams"):
            count["streams"] += 1
            if count["streams"] == 2:
                raise httpx.ConnectTimeout("down", request=request)
        return original(request)

    fake.mock_handler = second_stream_fails
    job = api.post("/api/campaigns", json=data).json()
    assert job["status"] == "partial" and job["campaign_id"] and job["geo_stream_id"] and not job["offer_stream_id"]
    assert job["last_error"]
    resumed = api.post(f"/api/campaign-jobs/{data['idempotency_key']}/resume").json()
    assert resumed["status"] == "done" and resumed["campaign_id"] == job["campaign_id"]
    assert len([s for s in fake.streams.values() if s["campaign_id"] == job["campaign_id"]]) == 2
    assert len([c for c in fake.campaigns.values() if c["alias"] == job["alias"]]) == 1


def test_created_campaign_opens_in_editor(api, fake):
    job = api.post("/api/campaigns", json=body()).json()
    c = api.post(f"/api/campaigns/{job['campaign_id']}/fetch", json={}).json()["campaign"]
    assert c["push_allowed"] is True
    geo, off = c["streams"]
    assert geo["links"] == [] and [(l["offer_id"], l["share"]) for l in off["links"]] == [(10184, 100)]


def test_parse_geo_unit():
    assert parse_geo(" au ") == ["AU"]
    assert parse_geo("MX,AU,MX") == ["MX", "AU"]
    with pytest.raises(ServiceError):
        parse_geo("USA")
