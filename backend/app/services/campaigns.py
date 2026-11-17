"""Создание кампании с двумя потоками.

Keitaro не даёт транзакции на несколько REST-вызовов, поэтому создание — пошаговое задание:
campaign → geo-поток → поток оффера → строгая проверка чтением.

- Выполнять задание может только владелец аренды (lease): захват — условным UPDATE, поэтому два
  параллельных start/resume не выполнят одинаковые POST. Зависшее после падения процесса задание
  становится доступным для resume по истечении аренды.
- Перед каждым POST и после неоднозначного/некорректного ответа — сверка чтением: кампания по
  заранее сгенерированному alias, поток по позиции и имени с обязательной проверкой полной формы.
  Несколько кандидатов или кандидат с другой формой — конфликт, а не выбор наугад.
- Любая ошибка завершает попытку контролируемым partial/failed. Автоматического удаления нет.
"""

import logging
import re
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..domain.countries import ISO_ALPHA2
from ..keitaro import payloads
from ..keitaro.client import KeitaroClient, KeitaroError
from ..keitaro.schemas import _canon, _canon_filters
from ..models import CampaignJob
from . import catalog
from .errors import ServiceError

log = logging.getLogger("keitaro_wrapper")

MAX_COUNTRIES = 20
NAME_RE = re.compile(r"^[\w\-. ]{1,120}$", re.UNICODE)
STREAM_SHAPE_FIELDS = ("campaign_id", "type", "name", "position", "state", "schema", "action_type", "action_payload")
DEFAULT_PORTS = {"http": 80, "https": 443}


class LeaseLost(Exception):
    """Аренду задания перехватил другой исполнитель — текущая попытка прекращается без записей."""


def parse_geo(raw: str) -> list[str]:
    """Одна страна — обязательный минимум; несколько через запятую — расширение (как в макете MX,AU,RO)."""
    codes = [c.strip().upper() for c in re.split(r"[,\s;]+", raw or "") if c.strip()]
    if not codes:
        raise ServiceError(422, "geo_required", "Укажите хотя бы один код страны (ISO 3166-1 alpha-2, например AU)")
    bad = [c for c in codes if c not in ISO_ALPHA2]
    if bad:
        hint = " (для Великобритании — GB)" if "UK" in bad else ""
        raise ServiceError(422, "geo_invalid", f"Неизвестные коды стран: {', '.join(bad)}{hint}")
    unique = list(dict.fromkeys(codes))
    if len(unique) > MAX_COUNTRIES:
        raise ServiceError(422, "geo_too_many", f"Не больше {MAX_COUNTRIES} стран")
    return unique


def normalize_name(raw: str, prefix: str) -> str:
    name = (raw or "").strip()
    if prefix and name.startswith(prefix):
        name = name[len(prefix):]
    if not name or not NAME_RE.fullmatch(name):
        raise ServiceError(422, "name_invalid", "Название: 1–120 символов, буквы, цифры, пробел, _ - .")
    return f"{prefix}{name}"


def has_space_or_control(value: str) -> bool:
    """Пробельные и управляющие символы недопустимы в URL. urlsplit молча удаляет \\t\\r\\n,
    поэтому проверяется исходная строка."""
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)


def normalize_http_url(value):
    """Каноническая форма HTTP(S)-URL для сравнения: регистр схемы и хоста, порт по умолчанию,
    завершающий «/» пути. Не-HTTP значения возвращаются без изменений."""
    if not isinstance(value, str):
        return value
    if has_space_or_control(value):
        return value  # не нормализуем: такое значение не может совпасть с корректным URL
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return value
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return value
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"  # IPv6: скобки обязательны, иначе «[::1]:8443» и «[::1:8443]» совпадут
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    if parts.username or parts.password:
        netloc = parts.netloc.rsplit("@", 1)[0] + "@" + netloc
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, parts.fragment))


def _shape_value(field: str, raw: dict):
    value = raw.get(field)
    if field == "action_payload":
        return _canon(normalize_http_url(value))
    if field == "campaign_id":
        try:
            return _canon(int(value))
        except (TypeError, ValueError):
            return _canon(value)
    return _canon(value)


def _offer_rows(offers) -> list[tuple]:
    rows = []
    for o in offers or []:
        if not isinstance(o, dict):
            return [("invalid",)]
        rows.append((o.get("offer_id"), o.get("share"), o.get("state") or "active"))
    return sorted(rows, key=repr)


def stream_shape_problems(raw: dict, expected: dict) -> list[str]:
    """Сравнить поток Keitaro с телом, которым он создавался: все значимые поля, фильтры и офферы."""
    if not isinstance(raw, dict):
        return ["поток не является объектом"]
    problems = [f"{f}: ожидалось {expected.get(f)!r}, получено {raw.get(f)!r}"
                for f in STREAM_SHAPE_FIELDS if _shape_value(f, raw) != _shape_value(f, expected)]
    if _canon_filters(raw.get("filters")) != _canon_filters(expected.get("filters", [])):
        problems.append(f"filters: ожидалось {expected.get('filters', [])!r}, получено {raw.get('filters')!r}")
    if _offer_rows(raw.get("offers")) != _offer_rows(expected.get("offers", [])):
        problems.append(f"offers: ожидалось {expected.get('offers', [])!r}, получено {raw.get('offers')!r}")
    return problems


def campaign_problems(camp, job: CampaignJob) -> list[str]:
    """Полная проверка кампании Keitaro против задания: type, name, alias и выбранные ID."""
    if not isinstance(camp, dict):
        return ["кампания не является объектом"]
    r = job.request
    expected = {"type": "position", "name": r["name"], "alias": job.alias, "domain_id": r["domain_id"],
                "group_id": r["group_id"], "traffic_source_id": r["traffic_source_id"]}
    problems = []
    for field, value in expected.items():
        got = camp.get(field)
        if field.endswith("_id"):
            try:
                got = int(got)
            except (TypeError, ValueError):
                got = None
        if got != value:
            problems.append(f"кампания.{field}: ожидалось {value!r}, получено {camp.get(field)!r}")
    return problems


class CampaignCreator:
    def __init__(self, db: Session, kt: KeitaroClient | None, settings: Settings):
        self.db = db
        self.kt = kt
        self.settings = settings

    def _require_kt(self) -> KeitaroClient:
        if self.kt is None:
            raise ServiceError(503, "keitaro_not_configured", "Keitaro не настроен: задайте KEITARO_URL и KEITARO_API_KEY")
        return self.kt

    def _require_prefix(self) -> str:
        prefix = self.settings.effective_prefix
        if not prefix:
            raise ServiceError(403, "prefix_not_configured",
                               "CAMPAIGN_NAME_PREFIX не задан или некорректен — создание кампаний запрещено")
        return prefix

    # --- входные данные ---

    def validate(self, data: dict) -> dict:
        prefix = self._require_prefix()
        kt = self._require_kt()
        name = normalize_name(data.get("name", ""), prefix)
        countries = parse_geo(data.get("geo", ""))
        try:
            offer_id = int(data.get("offer_id") or 0)
        except (TypeError, ValueError):
            offer_id = 0
        if offer_id <= 0:
            raise ServiceError(422, "invalid_offer_id", "Выберите оффер (положительный ID)")
        try:
            offer = kt.get_offer(offer_id)
        except KeitaroError as e:
            if e.kind == "not_found":
                raise ServiceError(422, "offer_not_found", f"Оффер {offer_id} не найден в Keitaro") from e
            raise
        if str(offer.get("state") or "active") in catalog.UNAVAILABLE_OFFER_STATES:
            raise ServiceError(422, "offer_unavailable", f"Оффер {offer_id} недоступен (state={offer.get('state')})")

        refs = catalog.references(kt)
        chosen = {}
        for field, key, default in (("domain_id", "domains", self.settings.default_domain_id),
                                    ("group_id", "groups", self.settings.default_group_id),
                                    ("traffic_source_id", "traffic_sources", self.settings.default_traffic_source_id)):
            value = data.get(field) or default
            allowed = {r["id"] for r in refs[key]}
            try:
                value = int(value) if value is not None else None
            except (TypeError, ValueError):
                value = None
            if value is None or value not in allowed:
                raise ServiceError(422, f"{field}_invalid", f"Выберите существующее значение {field} из списка Keitaro")
            chosen[field] = value
        return {"name": name, "countries": countries, "offer_id": offer_id, **chosen}

    # --- аренда ---

    def _claim(self, job_id: int) -> str | None:
        token, now = uuid.uuid4().hex, int(time.time())
        res = self.db.execute(
            update(CampaignJob)
            .where(CampaignJob.id == job_id,
                   or_(CampaignJob.status.in_(("pending", "partial", "failed")),
                       (CampaignJob.status == "running") & or_(CampaignJob.lease_until.is_(None), CampaignJob.lease_until < now)))
            .values(status="running", lease_token=token, lease_until=now + self.settings.job_lease_seconds)
        )
        self.db.commit()
        return token if res.rowcount == 1 else None

    def _renew(self, job_id: int, token: str) -> None:
        res = self.db.execute(
            update(CampaignJob)
            .where(CampaignJob.id == job_id, CampaignJob.lease_token == token, CampaignJob.status == "running")
            .values(lease_until=int(time.time()) + self.settings.job_lease_seconds)
        )
        self.db.commit()
        if res.rowcount != 1:
            raise LeaseLost()

    def _save(self, job_id: int, token: str, **values) -> None:
        res = self.db.execute(
            update(CampaignJob).where(CampaignJob.id == job_id, CampaignJob.lease_token == token).values(**values)
        )
        self.db.commit()
        if res.rowcount != 1:
            raise LeaseLost()

    # --- задание ---

    def _get(self, key: str) -> CampaignJob | None:
        return self.db.scalar(select(CampaignJob).where(CampaignJob.idempotency_key == key))

    def start(self, idempotency_key: str, data: dict) -> dict:
        key = (idempotency_key or "").strip()
        if not 8 <= len(key) <= 64:
            raise ServiceError(422, "idempotency_key_invalid", "Нужен idempotency_key (8–64 символа)")
        self._require_prefix()
        existing = self._get(key)
        if existing:
            return self.job_view(existing)  # повторный клик / повтор запроса — тот же результат
        kt = self._require_kt()  # до создания задания и захвата аренды
        request = self.validate(data)
        job = CampaignJob(idempotency_key=key, request=request, alias=payloads.new_alias(), status="pending")
        self.db.add(job)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return self.job_view(self._get(key))
        token = self._claim(job.id)
        if token is None:
            return self.job_view(self._reload(job))
        return self.run(job.id, token, kt)

    def resume(self, idempotency_key: str) -> dict:
        self._require_prefix()
        # Конфигурация Keitaro проверяется до захвата аренды: иначе задание осталось бы running без исполнителя.
        kt = self._require_kt()
        job = self._get(idempotency_key)
        if job is None:
            raise ServiceError(404, "job_not_found", "Задание не найдено")
        if job.status == "done":
            return self.job_view(job)
        token = self._claim(job.id)
        if token is None:
            raise ServiceError(409, "job_running", "Создание уже выполняется другим запросом", job=self.job_view(self._reload(job)))
        return self.run(job.id, token, kt)

    def _reload(self, job: CampaignJob) -> CampaignJob:
        self.db.expire_all()
        return self.db.get(CampaignJob, job.id)

    def run(self, job_id: int, token: str, kt: KeitaroClient) -> dict:
        """Выполнить задание под захваченной арендой. kt проверен вызывающим до захвата."""
        job = self.db.get(CampaignJob, job_id)
        self.db.refresh(job)
        r = job.request
        try:
            campaign_id = job.campaign_id or self._ensure_campaign(kt, job, token)
            if job.campaign_id is None:
                self._save(job.id, token, campaign_id=campaign_id)
            if job.geo_stream_id is None or job.offer_stream_id is None:
                # Перед любым POST /streams: кампания должна полностью совпадать с заданием.
                problems = campaign_problems(kt.get_campaign(campaign_id), job)
                if problems:
                    raise ServiceError(409, "reconcile_conflict",
                                       f"Кампания {campaign_id} не совпадает с заданием, потоки не создаются: " + "; ".join(problems))
            if job.geo_stream_id is None:
                body = payloads.geo_stream_payload(campaign_id=campaign_id, countries=r["countries"], redirect_url=self.settings.redirect_url)
                self._save(job.id, token, geo_stream_id=self._ensure_stream(kt, job, token, campaign_id, body))
            if job.offer_stream_id is None:
                body = payloads.offer_stream_payload(campaign_id=campaign_id, offer_id=r["offer_id"])
                self._save(job.id, token, offer_stream_id=self._ensure_stream(kt, job, token, campaign_id, body))
            self.db.refresh(job)
            self._verify(kt, job)
            status, error = "done", None
        except LeaseLost:
            return self.job_view(self._reload(job))
        except (KeitaroError, ServiceError) as e:
            status, error = None, getattr(e, "message", str(e))
        except Exception as e:  # noqa: BLE001 — задание не должно навсегда остаться running
            log.exception("campaign job %s failed", job_id)
            status, error = None, f"Внутренняя ошибка ({type(e).__name__}). Повтор начнёт со сверки уже созданного."
        self.db.rollback()
        job = self._reload(job)
        if status is None:
            status = "partial" if job.campaign_id else "failed"
        self.db.execute(
            update(CampaignJob).where(CampaignJob.id == job.id, CampaignJob.lease_token == token)
            .values(status=status, last_error=error, lease_token=None, lease_until=None)
        )
        self.db.commit()
        return self.job_view(self._reload(job))

    # --- шаги ---

    def _ensure_campaign(self, kt: KeitaroClient, job: CampaignJob, token: str) -> int:
        found = self._find_campaign(kt, job)
        if found:
            return found
        r = job.request
        body = payloads.campaign_payload(name=r["name"], alias=job.alias, group_id=r["group_id"],
                                         domain_id=r["domain_id"], traffic_source_id=r["traffic_source_id"])
        self._renew(job.id, token)
        try:
            created = kt.create_campaign(body)
            return int(created["id"])
        except KeitaroError as e:
            if not e.ambiguous:
                raise
            reason = e.message
        except (KeyError, TypeError, ValueError):
            reason = "Keitaro не вернул корректный id кампании"
        found = self._find_campaign(kt, job)
        if found:
            return found
        raise ServiceError(502, "create_unconfirmed",
                           f"Создание кампании не подтверждено ({reason}). «Продолжить» сначала сверит по alias — дубля не будет.")

    def _find_campaign(self, kt: KeitaroClient, job: CampaignJob) -> int | None:
        matches = []
        offset = 0
        while True:
            page = kt.list_campaigns(offset=offset, limit=500)
            matches += [c for c in page if isinstance(c, dict) and c.get("alias") == job.alias]
            if len(page) < 500:
                break
            offset += 500
        if not matches:
            return None
        if len(matches) > 1:
            raise ServiceError(409, "reconcile_conflict", f"В Keitaro несколько кампаний с alias {job.alias}; выбор не сделан")
        c = matches[0]
        problems = campaign_problems(c, job)
        if problems:
            raise ServiceError(409, "reconcile_conflict",
                               f"Кампания с alias {job.alias} не совпадает с заданием; не принимаю её как созданную: "
                               + "; ".join(problems))
        try:
            return int(c["id"])
        except (KeyError, TypeError, ValueError) as e:
            raise ServiceError(502, "reconcile_conflict", "Кампания найдена по alias, но без корректного id") from e

    @staticmethod
    def _find_stream(kt: KeitaroClient, campaign_id: int, body: dict) -> int | None:
        streams = kt.get_campaign_streams(campaign_id)
        candidates = [s for s in streams if isinstance(s, dict)
                      and s.get("position") == body["position"] and s.get("name") == body["name"]]
        if not candidates:
            return None
        if len(candidates) > 1:
            raise ServiceError(409, "reconcile_conflict",
                               f"Несколько потоков «{body['name']}» на позиции {body['position']}; выбор не сделан")
        problems = stream_shape_problems(candidates[0], body)
        if problems:
            raise ServiceError(409, "reconcile_conflict",
                               f"Поток «{body['name']}» найден, но не совпадает с ожидаемым: " + "; ".join(problems))
        try:
            return int(candidates[0]["id"])
        except (KeyError, TypeError, ValueError) as e:
            raise ServiceError(502, "reconcile_conflict", "Найденный поток без корректного id") from e

    def _ensure_stream(self, kt: KeitaroClient, job: CampaignJob, token: str, campaign_id: int, body: dict) -> int:
        found = self._find_stream(kt, campaign_id, body)
        if found:
            return found
        self._renew(job.id, token)
        try:
            return int(kt.create_stream(body)["id"])
        except KeitaroError as e:
            if not e.ambiguous:
                raise
            reason = e.message
        except (KeyError, TypeError, ValueError):
            reason = "Keitaro не вернул корректный id потока"
        found = self._find_stream(kt, campaign_id, body)
        if found:
            return found
        raise ServiceError(502, "create_unconfirmed",
                           f"Создание потока «{body['name']}» не подтверждено ({reason}). «Продолжить» сначала сверит потоки.")

    def _verify(self, kt: KeitaroClient, job: CampaignJob) -> None:
        r = job.request
        problems = campaign_problems(kt.get_campaign(job.campaign_id), job)

        streams = [s for s in kt.get_campaign_streams(job.campaign_id) if isinstance(s, dict)]
        ids = sorted(s.get("id") for s in streams)
        if ids != sorted([job.geo_stream_id, job.offer_stream_id]):
            problems.append(f"в кампании ожидались ровно потоки {job.geo_stream_id} и {job.offer_stream_id}, найдены {ids}")
        by_id = {s.get("id"): s for s in streams}
        expected = {
            job.geo_stream_id: payloads.geo_stream_payload(campaign_id=job.campaign_id, countries=r["countries"],
                                                           redirect_url=self.settings.redirect_url),
            job.offer_stream_id: payloads.offer_stream_payload(campaign_id=job.campaign_id, offer_id=r["offer_id"]),
        }
        for sid, body in expected.items():
            raw = by_id.get(sid)
            if raw is None:
                problems.append(f"поток {sid} не найден")
                continue
            problems += [f"поток {sid}: {p}" for p in stream_shape_problems(raw, body)]
        if problems:
            raise ServiceError(502, "verify_failed", "Проверка созданной кампании не пройдена: " + "; ".join(problems))

    def job_view(self, job: CampaignJob) -> dict:
        stale = job.status == "running" and (job.lease_until is None or job.lease_until < int(time.time()))
        return {
            "idempotency_key": job.idempotency_key,
            "status": job.status,
            "stale": stale,
            "name": job.request.get("name"),
            "countries": job.request.get("countries"),
            "offer_id": job.request.get("offer_id"),
            "alias": job.alias,
            "campaign_id": job.campaign_id,
            "geo_stream_id": job.geo_stream_id,
            "offer_stream_id": job.offer_stream_id,
            "last_error": job.last_error,
        }
