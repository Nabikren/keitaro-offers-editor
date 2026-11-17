"""Минимальные тела запросов на запись; ожидаемая форма потоков описана в README и проверяется тестами."""

import re
import secrets

GEO_STREAM_NAME = "GEO redirect"
OFFER_STREAM_NAME = "Offers"


def new_alias(prefix: str = "tsim") -> str:
    """Alias генерируется до POST и служит ключом сверки, если ответ на создание потерян."""
    return f"{prefix}{secrets.token_hex(5)}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def campaign_payload(*, name: str, alias: str, group_id: int, domain_id: int, traffic_source_id: int) -> dict:
    return {
        "alias": alias,
        "name": name,
        "type": "position",
        "group_id": group_id,
        "domain_id": domain_id,
        "traffic_source_id": traffic_source_id,
    }


def geo_stream_payload(*, campaign_id: int, countries: list[str], redirect_url: str) -> dict:
    """Поток 1: принимает указанные страны и редиректит на redirect_url."""
    return {
        "campaign_id": campaign_id,
        "type": "regular",
        "name": GEO_STREAM_NAME,
        "position": 1,
        "state": "active",
        "schema": "redirect",
        "action_type": "http",
        "action_payload": redirect_url,
        "filters": [{"name": "country", "mode": "accept", "payload": countries}],
    }


def offer_stream_payload(*, campaign_id: int, offer_id: int) -> dict:
    """Поток 2: без фильтров, весь остальной трафик на выбранный оффер (100%)."""
    return {
        "campaign_id": campaign_id,
        "type": "regular",
        "name": OFFER_STREAM_NAME,
        "position": 2,
        "state": "active",
        "schema": "landings",
        "action_type": "http",
        "action_payload": "",
        "filters": [],
        "offers": [{"offer_id": offer_id, "share": 100, "state": "active"}],
    }
