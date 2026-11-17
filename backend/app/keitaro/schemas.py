"""Разбор ответов Keitaro в узкие структуры. Чтение терпимо к расхождениям стенда и OAS
(payload фильтра — массив или строка; Offer.country — массив или пустая строка)."""

from dataclasses import dataclass, field
from typing import Any

from ..domain.links import RemoteOffer
from .client import KeitaroError

# Поля потока, которые редактор офферов не имеет права менять. Сравниваются до и после каждого PUT.
PROTECTED_STREAM_FIELDS = (
    "campaign_id", "type", "name", "position", "weight", "state",
    "schema", "action_type", "action_payload", "action_options",
    "collect_clicks", "filter_or", "filters",
    "triggers", "landings", "offer_selection", "comments",
)

# Служебные ключи вложенных объектов: идентификаторы строк, обратные ссылки и метки времени.
# Только они исключаются из сравнения; всё остальное сравнивается по значению.
SERVICE_KEYS = frozenset({"id", "oid", "stream_id", "created_at", "updated_at", "next_run_at"})


@dataclass
class StreamView:
    id: int
    campaign_id: int
    name: str
    position: int
    offers: list[RemoteOffer] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise KeitaroError("invalid_response", f"Некорректное поле {what}")
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise KeitaroError("invalid_response", f"Некорректное поле {what}") from e


def parse_remote_offers(raw_offers: Any) -> list[RemoteOffer]:
    if raw_offers in (None, ""):
        return []
    if not isinstance(raw_offers, list):
        raise KeitaroError("invalid_response", "Поле offers потока не является списком")
    result = []
    seen: set[int] = set()
    for item in raw_offers:
        if not isinstance(item, dict):
            raise KeitaroError("invalid_response", "Элемент offers потока не является объектом")
        offer_id = _int(item.get("offer_id"), "offer_id")
        if offer_id in seen:
            raise KeitaroError("invalid_response", f"Keitaro вернул повторяющуюся связь с оффером {offer_id} в одном потоке")
        seen.add(offer_id)
        result.append(
            RemoteOffer(
                offer_id=offer_id,
                share=_int(item.get("share", 0), "share"),
                state=str(item.get("state") or "active"),
                link_id=_int(item["id"], "id") if item.get("id") is not None else None,
            )
        )
    return result


def parse_stream(raw: Any) -> StreamView:
    if not isinstance(raw, dict) or "id" not in raw:
        raise KeitaroError("invalid_response", "Поток без id")
    return StreamView(
        id=_int(raw["id"], "stream.id"),
        campaign_id=_int(raw.get("campaign_id", 0), "stream.campaign_id"),
        name=str(raw.get("name") or f"Stream {raw['id']}"),
        position=_int(raw.get("position") or 0, "stream.position"),
        offers=parse_remote_offers(raw.get("offers")),
        raw=raw,
    )


def _canon(value: Any) -> Any:
    """Семантическая нормализация для сравнения.

    - None, "", [] и {} считаются одинаково пустыми;
    - числа сравниваются по значению (0 == 0.0), bool остаётся bool;
    - в словарях исключаются служебные ключи; порядок ключей не важен;
    - списки сравниваются без учёта порядка элементов.
    """
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, dict):
        items = {k: _canon(v) for k, v in value.items() if k not in SERVICE_KEYS}
        return ("dict", tuple(sorted((k, v) for k, v in items.items() if v is not None)))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(sorted((_canon(v) for v in value), key=repr)))
    return ("other", repr(value))


def _canon_filters(filters: Any) -> Any:
    """Фильтры: payload в чтении бывает массивом или строкой «a,b» — приводим к одному виду."""
    if not isinstance(filters, list):
        return _canon(filters)
    norm = []
    for f in filters:
        if isinstance(f, dict):
            f = dict(f)
            p = f.get("payload")
            if isinstance(p, str):
                f["payload"] = [x.strip() for x in p.replace("\n", ",").split(",") if x.strip()]
        norm.append(f)
    return _canon(norm)


def protected_view(raw: dict) -> dict:
    """Сравнимое представление полей потока, которые редактор не должен менять."""
    return {k: (_canon_filters(raw.get(k)) if k == "filters" else _canon(raw.get(k))) for k in PROTECTED_STREAM_FIELDS}


def protected_snapshot(raw: dict) -> dict:
    """Точная копия защищённых полей потока (как их вернул Keitaro) — для хранения в БД."""
    return {k: raw.get(k) for k in PROTECTED_STREAM_FIELDS}


def changed_protected_fields(before: dict, after: dict) -> list[str]:
    """Точный список защищённых полей, изменившихся между двумя чтениями потока."""
    a, b = protected_view(before), protected_view(after)
    return [k for k in PROTECTED_STREAM_FIELDS if a[k] != b[k]]


def offer_countries(raw_offer: dict) -> list[str]:
    value = raw_offer.get("country")
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
