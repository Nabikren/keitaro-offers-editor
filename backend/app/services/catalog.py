"""Каталог офферов (кэш) и справочники для формы. Поиск идёт через наш backend: в Keitaro
GET /offers не имеет фильтров, поэтому каталог кэшируется и ищется локально."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..keitaro.client import KeitaroClient
from ..models import Offer

CACHE_TTL = timedelta(minutes=5)
UNAVAILABLE_OFFER_STATES = {"deleted", "archived", "disabled"}


def upsert_offers(db: Session, raw_offers: list[dict]) -> int:
    now = datetime.now(timezone.utc)
    existing = {o.id: o for o in db.scalars(select(Offer))}
    count = 0
    for raw in raw_offers:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        oid = int(raw["id"])
        offer = existing.get(oid) or Offer(id=oid)
        offer.name = str(raw.get("name") or "")
        offer.state = str(raw.get("state") or "")
        offer.synced_at = now
        db.add(offer)
        count += 1
    return count


def ensure_offer_cache(db: Session, kt: KeitaroClient, force: bool = False) -> None:
    newest = db.scalar(select(func.max(Offer.synced_at)))
    if newest is not None and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    if force or newest is None or datetime.now(timezone.utc) - newest > CACHE_TTL:
        upsert_offers(db, kt.list_offers())
        db.commit()


def search_offers(db: Session, query: str, limit: int = 20) -> list[Offer]:
    q = query.strip()
    stmt = select(Offer).where(Offer.state.notin_(UNAVAILABLE_OFFER_STATES))
    if q:
        conds = [func.lower(Offer.name).contains(q.lower())]
        if q.isdigit():
            conds.append(Offer.id == int(q))
        stmt = stmt.where(or_(*conds))
    rows = list(db.scalars(stmt.limit(500)))
    # Точное совпадение ID — первым, затем по ID.
    rows.sort(key=lambda o: (0 if q.isdigit() and o.id == int(q) else 1, o.id))
    return rows[:limit]


def offer_dict(o: Offer) -> dict:
    return {"id": o.id, "name": o.name, "state": o.state}


def references(kt: KeitaroClient) -> dict:
    def active(items):
        return [{"id": int(i["id"]), "name": str(i.get("name") or i["id"])} for i in items
                if isinstance(i, dict) and i.get("id") is not None and str(i.get("state") or "active") == "active"]

    groups = [{"id": int(g["id"]), "name": str(g.get("name") or g["id"])} for g in kt.list_campaign_groups()
              if isinstance(g, dict) and g.get("id") is not None]
    return {"domains": active(kt.list_domains()), "groups": groups, "traffic_sources": active(kt.list_traffic_sources())}
