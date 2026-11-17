"""Контракт клиента: заголовок Api-Key, путь /admin_api/v1, минимальные тела записи,
классификация ошибок и неоднозначных результатов. Сеть не используется."""

import json

import httpx
import pytest

from app.keitaro import payloads
from app.keitaro.client import KeitaroClient, KeitaroError
from app.keitaro.schemas import parse_stream, protected_view


def client_with(handler) -> KeitaroClient:
    return KeitaroClient("https://kt.example", "secret-key-123", timeout=2, transport=httpx.MockTransport(handler))


def test_uses_api_key_header_and_v1_prefix():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("Api-Key")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    client_with(handler).list_offers()
    assert seen == {"path": "/admin_api/v1/offers", "key": "secret-key-123", "auth": None}


def test_groups_requested_with_campaigns_type():
    def handler(request):
        assert request.url.params["type"] == "campaigns"
        return httpx.Response(200, json=[])

    client_with(handler).list_campaign_groups()


def test_missing_config_is_explicit_error():
    with pytest.raises(KeitaroError) as e:
        KeitaroClient("", "")
    assert e.value.kind == "config"


@pytest.mark.parametrize("status, kind", [(401, "auth"), (403, "auth"), (402, "unavailable"), (404, "not_found"), (400, "bad_request"), (500, "server"), (503, "server")])
def test_http_errors_are_classified(status, kind):
    client = client_with(lambda r: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(KeitaroError) as e:
        client.get_stream(1)
    assert e.value.kind == kind and e.value.status == status
    assert not e.value.ambiguous  # чтение никогда не бывает неоднозначным


def test_error_messages_never_contain_api_key():
    client = client_with(lambda r: httpx.Response(401, json={"error": "Access denied"}))
    with pytest.raises(KeitaroError) as e:
        client.list_offers()
    assert "secret-key-123" not in str(e.value) and "secret-key-123" not in repr(client)


def test_read_timeout_on_write_is_ambiguous():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(KeitaroError) as e:
        client_with(handler).update_stream_offers(1, [])
    assert e.value.kind == "timeout" and e.value.ambiguous


def test_connect_timeout_on_write_is_not_ambiguous():
    def handler(request):
        raise httpx.ConnectTimeout("no route", request=request)

    with pytest.raises(KeitaroError) as e:
        client_with(handler).create_stream({})
    assert e.value.kind == "timeout" and not e.value.ambiguous


def test_5xx_on_write_is_ambiguous():
    with pytest.raises(KeitaroError) as e:
        client_with(lambda r: httpx.Response(502)).create_campaign({})
    assert e.value.kind == "server" and e.value.ambiguous


def test_non_json_and_wrong_shape_are_invalid_response():
    with pytest.raises(KeitaroError) as e:
        client_with(lambda r: httpx.Response(200, content=b"<html>")).list_offers()
    assert e.value.kind == "invalid_response"
    with pytest.raises(KeitaroError) as e:
        client_with(lambda r: httpx.Response(200, json={"x": 1})).list_offers()
    assert e.value.kind == "invalid_response"


def test_put_sends_only_offers():
    sent = {}

    def handler(request):
        sent["method"] = request.method
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7})

    client_with(handler).update_stream_offers(7, [{"offer_id": 1, "share": 100, "state": "active"}])
    assert sent == {"method": "PUT", "body": {"offers": [{"offer_id": 1, "share": 100, "state": "active"}]}}


def test_campaign_payload_is_minimal_and_matches_required_fields():
    body = payloads.campaign_payload(name="test_simonov_x", alias="tsimabc", group_id=9799, domain_id=1813, traffic_source_id=836)
    assert body == {"alias": "tsimabc", "name": "test_simonov_x", "type": "position", "group_id": 9799, "domain_id": 1813, "traffic_source_id": 836}


def test_stream_payloads_match_stand_shape():
    geo = payloads.geo_stream_payload(campaign_id=5, countries=["AU", "MX"], redirect_url="https://www.google.com/")
    assert geo["position"] == 1 and geo["schema"] == "redirect" and geo["action_type"] == "http"
    assert geo["filters"] == [{"name": "country", "mode": "accept", "payload": ["AU", "MX"]}]
    assert "offers" not in geo
    off = payloads.offer_stream_payload(campaign_id=5, offer_id=10184)
    assert off["position"] == 2 and off["schema"] == "landings" and off["filters"] == []
    assert off["offers"] == [{"offer_id": 10184, "share": 100, "state": "active"}]
    for body in (geo, off):
        assert not {"id", "created_at", "updated_at"} & set(body)
        assert {"campaign_id", "schema", "type", "name", "action_type"} <= set(body)


def test_parse_stream_tolerates_stand_variants():
    raw = {"id": 1, "campaign_id": 2, "name": "Flow", "position": 1,
           "filters": [{"name": "country", "mode": "accept", "payload": ["AU"]}],
           "offers": [{"id": 9, "offer_id": 3, "share": 33, "state": "active"}]}
    view = parse_stream(raw)
    assert [(o.offer_id, o.share, o.link_id) for o in view.offers] == [(3, 33, 9)]
    assert parse_stream({"id": 1, "offers": None}).offers == []
    with pytest.raises(KeitaroError):
        parse_stream({"id": 1, "offers": [{"offer_id": "x", "share": 1}]})


def test_protected_view_ignores_ids_and_payload_format():
    a = {"type": "regular", "filters": [{"id": 1, "name": "country", "mode": "accept", "payload": ["MX", "AU"]}], "offers": [1]}
    b = {"type": "regular", "filters": [{"id": 2, "name": "country", "mode": "accept", "payload": "AU,MX"}], "offers": [2]}
    assert protected_view(a) == protected_view(b)
    c = dict(a, filters=[])
    assert protected_view(a) != protected_view(c)
