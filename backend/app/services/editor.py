"""Редактор офферов кампании: Fetch / правки черновика / Cancel / Push.

Согласованность без опоры на блокировки конкретной СУБД:
- у каждого потока есть draft_version; любая запись (правка, захват Push, применение Fetch,
  подтверждение Push) выполняется условным UPDATE по ожидаемой версии (CAS);
- HTTP-вызовы к Keitaro не выполняются внутри транзакции: сначала фиксируется снимок версий,
  затем идёт сеть, затем результат применяется только если версии не изменились.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..domain import links as L
from ..domain.links import EditorError
from ..keitaro.client import KeitaroClient, KeitaroError
from ..keitaro.schemas import StreamView, changed_protected_fields, parse_stream, protected_snapshot
from ..models import Campaign, CampaignJob, Offer, Stream, StreamOffer
from . import catalog
from .errors import ServiceError

EDITOR_ERROR_STATUS = {"pin_out_of_range": 422}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _factory(stream_id: int):
    def make(offer_id: int, position: int) -> StreamOffer:
        return StreamOffer(stream_id=stream_id, offer_id=offer_id, position=position, status="active", share=0,
                           pinned=False, kt_state="active", pub_present=False, pub_pinned=False, ever_published=False)

    return make


def link_view_state(l: StreamOffer) -> str:
    if l.status == "removed":
        return "pending_remove" if l.pub_present else "archived"
    if not l.pub_present:
        return "restoring" if l.ever_published else "new"
    return "changed" if L.is_link_dirty(l) else "published"


class EditorService:
    def __init__(self, db: Session, kt: KeitaroClient | None, settings: Settings):
        self.db = db
        self.kt = kt
        self.settings = settings

    # --- представление ---

    def _require_kt(self) -> KeitaroClient:
        if self.kt is None:
            raise ServiceError(503, "keitaro_not_configured", "Keitaro не настроен: задайте KEITARO_URL и KEITARO_API_KEY")
        return self.kt

    def _name_allowed(self, name: str | None) -> bool:
        prefix = self.settings.effective_prefix
        return bool(prefix) and isinstance(name, str) and name.startswith(prefix)

    def stream_view(self, s: Stream) -> dict:
        names = {o.id: o for o in self.db.scalars(select(Offer).where(Offer.id.in_([l.offer_id for l in s.links])))}
        links = L.ordered(s.links)
        return {
            "id": s.id,
            "name": s.name,
            "position": s.position,
            "has_offers": bool(links),
            "dirty": L.is_dirty(links),
            "draft_version": s.draft_version,
            "push_state": s.push_state,
            "push_stale": self._push_stale(s),  # только предупреждение; перехвата нет
            "last_error": s.last_error,
            "side_effect_fields": s.side_effect_fields or [],
            # Результат прошлого Push неизвестен: есть снимок перед PUT. Сам снимок наружу не отдаётся.
            "reconciliation_required": s.push_baseline is not None,
            "remote_missing": s.remote_missing,
            "synced_at": _aware(s.synced_at).isoformat() if s.synced_at else None,
            "active_sum": sum(l.share for l in links if l.status == "active" and l.kt_state == "active"),
            "links": [
                {
                    "offer_id": l.offer_id,
                    "offer_name": names[l.offer_id].name if l.offer_id in names else "",
                    "offer_state": names[l.offer_id].state if l.offer_id in names else "",
                    "status": l.status,
                    "share": l.share,
                    "pinned": l.pinned,
                    "kt_state": l.kt_state,
                    "pub_present": l.pub_present,
                    "pub_share": l.pub_share,
                    "view_state": link_view_state(l),
                    "dirty": L.is_link_dirty(l),
                }
                for l in links
            ],
        }

    def campaign_view(self, campaign_id: int) -> dict:
        c = self.db.get(Campaign, campaign_id)
        if c is None:
            raise ServiceError(404, "campaign_not_loaded", "Кампания ещё не загружена: нажмите Fetch streams")
        return {
            "id": c.id,
            "name": c.name,
            "alias": c.alias,
            "synced_at": _aware(c.synced_at).isoformat() if c.synced_at else None,
            "push_allowed": self._name_allowed(c.name),
            "streams": [self.stream_view(s) for s in c.streams],
        }

    def list_local(self) -> list[dict]:
        return [{"id": c.id, "name": c.name, "synced_at": _aware(c.synced_at).isoformat() if c.synced_at else None}
                for c in self.db.scalars(select(Campaign).order_by(Campaign.synced_at.desc()))]

    def _fresh_campaign_view_or_none(self, campaign_id: int) -> dict | None:
        self.db.expire_all()
        try:
            return self.campaign_view(campaign_id)
        except ServiceError:
            return None

    # --- Fetch ---

    def _push_stale(self, s: Stream) -> bool:
        """Только для предупреждения: возраст НЕ даёт права перехватить запись (иначе два исполнителя
        могли бы одновременно писать в Keitaro). Зависший Push снимается офлайн-сверкой при остановленном backend."""
        started = _aware(s.push_started_at)
        return s.push_state == "pushing" and (started is None or _now() - started > timedelta(seconds=self.settings.push_stale_seconds))

    def _push_in_progress(self, streams: list[Stream], what: str) -> ServiceError:
        stale = [x.id for x in streams if self._push_stale(x)]
        message = f"Идёт публикация потока — {what} недоступен до её завершения."
        if stale:
            message += (f" Публикация потоков {stale} длится дольше {self.settings.push_stale_seconds} с: если backend "
                        "аварийно завершался, остановите его и выполните офлайн-сверку `python -m app.recover`.")
        payload = {"streams": [x.id for x in streams], "stale_streams": stale}
        if len(streams) == 1:
            payload["stream"] = self.stream_view(streams[0])
        return ServiceError(423, "push_in_progress", message, **payload)

    def fetch(self, campaign_id: int, discard_drafts: bool = False) -> dict:
        kt = self._require_kt()

        # 1. Снимок локальных версий до сетевых вызовов.
        local = list(self.db.scalars(select(Stream).where(Stream.campaign_id == campaign_id)))
        pushing = [s for s in local if s.push_state == "pushing"]  # любой возраст: перехвата нет
        if pushing:
            raise self._push_in_progress(pushing, "Fetch")
        dirty = [s.id for s in local if L.is_dirty(s.links)]
        if dirty and not discard_drafts:
            raise ServiceError(409, "dirty_drafts", "Есть неопубликованные изменения. Fetch заменит их данными Keitaro.", streams=dirty)
        snapshot = {s.id: (s.draft_version, s.push_state) for s in local}
        baselines = {s.id: s.push_baseline for s in local if s.push_baseline is not None}
        self.db.rollback()  # не держим транзакцию во время HTTP

        # 2. Чтение Keitaro (дубли связей и некорректные данные — явная ошибка до любых изменений).
        raw_campaign = kt.get_campaign(campaign_id)
        remote: list[StreamView] = [parse_stream(r) for r in kt.get_campaign_streams(campaign_id)]
        # Поток с неподтверждённым Push исчез из списка кампании: читаем его по ID, чтобы завершить сверку
        # (прошлый PUT мог перенести его в другую кампанию). 404 — поток действительно удалён.
        listed = {v.id for v in remote}
        missing_reads: dict[int, dict | None] = {}
        for sid in baselines:
            if sid not in listed:
                try:
                    missing_reads[sid] = kt.get_stream(sid)
                except KeitaroError as e:
                    if e.kind != "not_found":
                        raise
                    missing_reads[sid] = None
        try:
            offers_raw = kt.list_offers()
        except KeitaroError:
            offers_raw = []  # имена офферов не критичны для импорта связей

        # 3. Применение только если с момента снимка ни один поток не менялся (CAS по версии).
        for sid, (version, state) in snapshot.items():
            res = self.db.execute(
                update(Stream)
                .where(Stream.id == sid, Stream.draft_version == version, Stream.push_state == state)
                .values(draft_version=version + 1)
            )
            if res.rowcount != 1:
                self.db.rollback()
                raise ServiceError(409, "concurrent_change",
                                   "Поток изменился во время Fetch — данные Keitaro не применены, ваша правка сохранена.",
                                   stream_id=sid, campaign=self._fresh_campaign_view_or_none(campaign_id))
        new_ids = [v.id for v in remote if v.id not in snapshot]
        if new_ids and self.db.scalar(select(Stream.id).where(Stream.id.in_(new_ids)).limit(1)) is not None:
            self.db.rollback()
            raise ServiceError(409, "concurrent_change", "Параллельный Fetch уже импортировал потоки. Обновите страницу.",
                               campaign=self._fresh_campaign_view_or_none(campaign_id))

        catalog.upsert_offers(self.db, offers_raw)
        c = self.db.get(Campaign, campaign_id) or Campaign(id=campaign_id)
        c.name = str(raw_campaign.get("name") or "")
        c.alias = str(raw_campaign.get("alias") or "")
        c.synced_at = _now()
        self.db.add(c)

        report = {"added": {}, "removed_externally": {}, "side_effects": {}, "discarded": dirty if discard_drafts else []}
        seen = set()
        try:
            for view in remote:
                seen.add(view.id)
                s = self.db.get(Stream, view.id)
                if s is None:
                    s = Stream(id=view.id, campaign_id=campaign_id, draft_version=1, push_state="idle", remote_missing=False)
                    s.links = []
                    self.db.add(s)
                else:
                    self.db.refresh(s)
                if s.campaign_id != campaign_id:
                    raise ServiceError(409, "stream_campaign_mismatch", f"Поток {view.id} принадлежит другой кампании")
                if L.is_dirty(s.links):
                    for gone in L.cancel(s.links):
                        s.links.remove(gone)
                    # Удаление отменённых связей должно попасть в БД раньше, чем Keitaro вернёт ту же связь
                    # (иначе INSERT опередит DELETE и нарушит UNIQUE(stream_id, offer_id)).
                    self.db.flush()
                s.name, s.position = view.name, view.position
                s.remote_snapshot = view.raw
                s.remote_missing = False
                s.push_state, s.push_started_at, s.last_error = "idle", None, None
                # Повторный Fetch — явная пересинхронизация. Но если остался снимок перед неподтверждённым
                # PUT, сначала сравниваем с ним: побочный эффект той записи не должен потеряться.
                drift = changed_protected_fields(s.push_baseline, view.raw) if s.push_baseline else []
                s.side_effect_fields = drift or None
                s.push_baseline = None
                if drift:
                    s.last_error = ("Fetch: после неподтверждённого Push изменились защищённые поля потока: "
                                    + ", ".join(drift) + ". Проверьте поток в Keitaro; Push заблокирован до следующего Fetch.")
                    report["side_effects"][view.id] = drift
                s.synced_at = _now()
                result = L.merge_remote(s.links, view.offers, _factory(view.id))
                for key in ("added", "removed_externally"):
                    if getattr(result, key):
                        report[key][view.id] = getattr(result, key)
            for sid in snapshot:
                if sid not in seen:
                    s = self.db.get(Stream, sid)
                    s.remote_missing = True
                    if sid in baselines:
                        s.push_state, s.push_started_at = "idle", None
                        raw = missing_reads.get(sid)
                        if raw is None:
                            s.side_effect_fields = None
                            s.last_error = "Поток удалён в Keitaro; сверка неподтверждённого Push завершена."
                        else:
                            drift = changed_protected_fields(s.push_baseline, raw)
                            s.side_effect_fields = drift or None
                            s.last_error = ("Поток больше не входит в кампанию. После неподтверждённого Push изменились "
                                            "защищённые поля: " + (", ".join(drift) or "нет") + ". Проверьте поток в Keitaro.")
                            if drift:
                                report["side_effects"][sid] = drift
                        s.push_baseline = None
        except EditorError as e:
            self.db.rollback()
            raise ServiceError(409, e.code, e.message) from e
        except ServiceError:
            self.db.rollback()
            raise
        self.db.commit()
        return {"campaign": self.campaign_view(campaign_id), "report": report}

    # --- правки черновика ---

    def _claim_edit(self, stream_id: int, expected_version: int) -> Stream:
        """Атомарно проверить версию и отсутствие Push, увеличить версию. Защищает от гонок и устаревших запросов."""
        res = self.db.execute(
            update(Stream)
            .where(Stream.id == stream_id, Stream.draft_version == expected_version, Stream.push_state == "idle")
            .values(draft_version=Stream.draft_version + 1)
        )
        if res.rowcount == 0:
            self.db.rollback()
            s = self.db.get(Stream, stream_id)
            if s is None:
                raise ServiceError(404, "stream_not_found", "Поток не загружен")
            self.db.refresh(s)
            if s.push_state == "pushing":
                raise self._push_in_progress([s], "правка")
            raise ServiceError(409, "stale_version", "Поток изменился в другой вкладке/запросе. Данные обновлены.",
                               stream=self.stream_view(s))
        s = self.db.get(Stream, stream_id)
        self.db.refresh(s)
        if s.remote_missing:
            self.db.rollback()
            raise ServiceError(409, "stream_missing", "Поток удалён в Keitaro, правки недоступны")
        # Снимок появляется только под захватом Push (версия меняется), поэтому после нашего CAS по версии
        # проверка в этой же транзакции согласована и не зависит от блокировок СУБД.
        if s.push_baseline is not None:
            self.db.rollback()
            self.db.refresh(s)
            raise ServiceError(409, "reconciliation_required",
                               "Результат предыдущего Push неизвестен. Сначала повторите Push (он сверит состояние Keitaro) "
                               "или сделайте Fetch; до этого правки и Cancel недоступны.", stream=self.stream_view(s))
        return s

    def edit(self, stream_id: int, expected_version: int, op: str, offer_id: int | None = None, share: int | None = None) -> dict:
        if op == "add":
            # Дешёвая предварительная проверка без сети: заблокированный Add не обращается к Keitaro.
            # Окончательная проверка — в _claim_edit после CAS (снимок мог появиться, пока шёл запрос).
            current = self.db.get(Stream, stream_id)
            if current is not None and current.push_baseline is not None:
                raise ServiceError(409, "reconciliation_required",
                                   "Результат предыдущего Push неизвестен. Сначала повторите Push (он сверит состояние Keitaro) "
                                   "или сделайте Fetch; до этого правки и Cancel недоступны.", stream=self.stream_view(current))
            self.db.rollback()
            self._validate_offer_available(offer_id)
        s = self._claim_edit(stream_id, expected_version)
        try:
            if op == "add":
                L.add(s.links, offer_id, _factory(s.id))
            elif op == "remove":
                L.remove(s.links, offer_id)
            elif op == "restore":
                L.restore(s.links, offer_id)
            elif op == "pin":
                L.pin(s.links, offer_id, share)
            elif op == "unpin":
                L.unpin(s.links, offer_id)
            elif op == "cancel":
                for gone in L.cancel(s.links):
                    s.links.remove(gone)
            else:
                raise ServiceError(400, "unknown_op", op)
        except EditorError as e:
            self.db.rollback()
            raise ServiceError(EDITOR_ERROR_STATUS.get(e.code, 409), e.code, e.message) from e
        L.sync_pin_baseline_if_clean(s.links)
        if not s.side_effect_fields and s.push_baseline is None:
            s.last_error = None
        self.db.commit()
        return self.stream_view(s)

    def _validate_offer_available(self, offer_id: int | None) -> None:
        if not offer_id or offer_id <= 0:
            raise ServiceError(422, "invalid_offer_id", "Укажите положительный ID оффера")
        kt = self._require_kt()
        try:
            raw = kt.get_offer(offer_id)
        except KeitaroError as e:
            if e.kind == "not_found":
                raise ServiceError(422, "offer_not_found", f"Оффер {offer_id} не найден в Keitaro") from e
            raise
        catalog.upsert_offers(self.db, [raw])
        self.db.commit()
        state = str(raw.get("state") or "active")
        if state in catalog.UNAVAILABLE_OFFER_STATES:
            raise ServiceError(422, "offer_unavailable", f"Оффер {offer_id} недоступен (state={state})")

    # --- Push ---

    def _forbidden(self, message: str) -> ServiceError:
        return ServiceError(403, "push_forbidden", message)

    def push(self, stream_id: int, expected_version: int) -> dict:
        kt = self._require_kt()
        s = self.db.get(Stream, stream_id)
        if s is None:
            raise ServiceError(404, "stream_not_found", "Поток не загружен")
        if s.push_state == "pushing":  # любой возраст: второй Push не перехватывает запись
            raise self._push_in_progress([s], "повторный Push")
        prefix = self.settings.effective_prefix
        if not prefix:
            raise self._forbidden("CAMPAIGN_NAME_PREFIX не задан или некорректен — публикация запрещена")
        if not self._name_allowed(s.campaign.name):
            raise self._forbidden(f"Публикация разрешена только для кампаний с префиксом {prefix!r}")
        if s.side_effect_fields:
            raise ServiceError(409, "side_effect_unresolved",
                               "После прошлого Push изменились защищённые поля потока. Проверьте поток в Keitaro и сделайте Fetch.",
                               stream=self.stream_view(s))
        if s.draft_version != expected_version:
            raise ServiceError(409, "stale_version", "Черновик изменился, обновите данные", stream=self.stream_view(s))
        if not L.is_dirty(s.links):
            return {"stream": self.stream_view(s), "result": "nothing_to_push"}

        # Захват через CAS по версии и только из idle: из двух параллельных Push пройдёт только один,
        # а зависший pushing не перехватывается никогда (см. app/recover.py).
        claimed = expected_version + 1
        res = self.db.execute(
            update(Stream)
            .where(Stream.id == stream_id, Stream.draft_version == expected_version, Stream.push_state == "idle")
            .values(push_state="pushing", push_started_at=_now(), draft_version=claimed)
        )
        self.db.commit()
        if res.rowcount == 0:
            self.db.refresh(s)
            if s.push_state == "pushing":
                raise self._push_in_progress([s], "повторный Push")
            raise ServiceError(409, "stale_version", "Поток изменился, Push не начат", stream=self.stream_view(s))
        self.db.refresh(s)
        local_campaign_id = s.campaign_id
        desired = L.desired_offers(s.links)

        # Чтение перед записью: сначала поток.
        try:
            before = parse_stream(kt.get_stream(stream_id))
        except KeitaroError as e:
            self._release(s, claimed, f"Push не выполнен: не удалось прочитать поток ({e.message})")
            raise

        # Снимок защищённых полей перед первым PUT неподтверждённого Push (из прошлой попытки) или None.
        # Сверка с ним — сразу после чтения потока, до проверок кампании: прошлый PUT мог изменить
        # и campaign_id, и тогда _guard_target остановил бы Push, не разрешив неизвестный результат.
        baseline = s.push_baseline
        if baseline is not None:
            drift = changed_protected_fields(baseline, before.raw)
            if drift:
                if L.desired_matches(s.links, before.offers):
                    # Офферы прошлой записи применились: фиксируем их и побочный эффект, второй PUT не нужен.
                    return self._confirm(s, claimed, baseline, before, result="already_applied")
                self._record_side_effect(s, claimed, drift)
                raise ServiceError(409, "side_effect_detected",
                                   "После прошлой неподтверждённой записи изменились защищённые поля потока: "
                                   + ", ".join(drift) + ". Повторный PUT не выполнен.",
                                   changed_fields=drift, stream=self.stream_view(s))

        try:
            remote_campaign = kt.get_campaign(local_campaign_id)
        except KeitaroError as e:
            self._release(s, claimed, f"Push не выполнен: не удалось прочитать кампанию ({e.message})")
            raise
        self._guard_target(s, claimed, before, remote_campaign)

        if L.desired_matches(s.links, before.offers):
            # Предыдущая неоднозначная попытка уже применилась — повторно не пишем, но сравниваем
            # текущий поток со снимком, сделанным до той записи.
            reference = baseline if baseline is not None else protected_snapshot(before.raw)
            return self._confirm(s, claimed, reference, before, result="already_applied")
        if not L.published_matches(s.links, before.offers):
            self._release(s, claimed, "Состав офферов в Keitaro изменился после последнего Fetch")
            raise ServiceError(409, "remote_changed",
                               "Keitaro изменён вне редактора. Сделайте Fetch (черновик можно отменить или заменить).",
                               stream=self.stream_view(s))

        # Перед записью фиксируем в БД точный снимок защищённых полей (только своим захватом).
        created_baseline = baseline is None
        if created_baseline:
            baseline = protected_snapshot(before.raw)
            self.db.rollback()
            res = self.db.execute(
                update(Stream)
                .where(Stream.id == stream_id, Stream.draft_version == claimed, Stream.push_state == "pushing")
                .values(push_baseline=baseline)
            )
            self.db.commit()
            if res.rowcount != 1:
                self.db.refresh(s)
                raise ServiceError(409, "push_superseded", "Push прерван: поток был пересинхронизирован до записи",
                                   stream=self.stream_view(s))
            self.db.refresh(s)

        write_error: KeitaroError | None = None
        try:
            kt.update_stream_offers(stream_id, desired)
        except KeitaroError as e:
            if not e.ambiguous:
                # Запись точно не применилась: снимок этой попытки больше не нужен.
                self._release(s, claimed, f"Push отклонён: {e.message}", clear_baseline=created_baseline)
                raise
            write_error = e  # результат неизвестен → сверка чтением

        try:
            after = parse_stream(kt.get_stream(stream_id))
        except KeitaroError as e:
            self._release(s, claimed, f"Результат Push неизвестен: проверка чтением не удалась ({e.message}). "
                                      "Повтор Push сначала сверит состояние.")
            raise ServiceError(502, "push_unconfirmed", "Результат Push не подтверждён, черновик сохранён",
                               stream=self.stream_view(s)) from e

        if not L.desired_matches(s.links, after.offers):
            msg = "Push не подтвердился: в Keitaro другой состав офферов"
            if write_error:
                msg = f"Push не выполнен ({write_error.message}); проверка показала, что изменения не применены"
            self._release(s, claimed, msg)
            raise ServiceError(502, "push_unconfirmed", msg, stream=self.stream_view(s))
        return self._confirm(s, claimed, baseline, after, result="published")

    def _guard_target(self, s: Stream, claimed: int, before: StreamView, remote_campaign: dict) -> None:
        """Перед записью убедиться, что пишем в тот же поток той же собственной кампании."""
        def stop(error: ServiceError) -> None:
            self._release(s, claimed, error.message)
            raise error

        if before.campaign_id != s.campaign_id:
            stop(ServiceError(409, "remote_campaign_mismatch",
                              f"Поток {s.id} в Keitaro принадлежит кампании {before.campaign_id}, а не {s.campaign_id}. Push остановлен."))
        try:
            remote_id = int(remote_campaign.get("id"))
        except (TypeError, ValueError):
            remote_id = None
        if remote_id != s.campaign_id:
            stop(ServiceError(409, "remote_campaign_mismatch", "Keitaro вернул другую кампанию. Push остановлен."))
        remote_name = remote_campaign.get("name")
        if not self._name_allowed(remote_name):
            stop(self._forbidden(f"Актуальное имя кампании в Keitaro {remote_name!r} не начинается с префикса "
                                 f"{self.settings.effective_prefix!r}. Push остановлен."))
        job_aliases = {a for (a,) in self.db.execute(select(CampaignJob.alias).where(CampaignJob.campaign_id == s.campaign_id))}
        if job_aliases and remote_campaign.get("alias") not in job_aliases:
            stop(ServiceError(409, "alias_mismatch",
                              "Alias кампании в Keitaro не совпадает с alias, под которым её создало приложение. Push остановлен."))

    def _release(self, s: Stream, claimed: int, error: str, clear_baseline: bool = False) -> None:
        """Снять захват Push, только если он всё ещё наш (иначе ничего не трогаем).
        Снимок push_baseline сохраняется, если запись могла примениться."""
        self.db.rollback()
        values = {"push_state": "idle", "push_started_at": None, "last_error": error}
        if clear_baseline:
            values["push_baseline"] = None
        self.db.execute(
            update(Stream)
            .where(Stream.id == s.id, Stream.draft_version == claimed, Stream.push_state == "pushing")
            .values(**values)
        )
        self.db.commit()
        self.db.refresh(s)

    def _record_side_effect(self, s: Stream, claimed: int, changed: list[str]) -> None:
        self.db.rollback()
        self.db.execute(
            update(Stream)
            .where(Stream.id == s.id, Stream.draft_version == claimed, Stream.push_state == "pushing")
            .values(push_state="idle", push_started_at=None, push_baseline=None, side_effect_fields=changed,
                    last_error="СТОП: изменились защищённые поля потока: " + ", ".join(changed)
                               + ". Проверьте поток в Keitaro; Push заблокирован до Fetch.")
        )
        self.db.commit()
        self.db.refresh(s)

    def _confirm(self, s: Stream, claimed: int, baseline: dict, after: StreamView, result: str) -> dict:
        """baseline — защищённые поля до первой записи этого Push (из БД или текущего чтения)."""
        self.db.rollback()
        res = self.db.execute(
            update(Stream)
            .where(Stream.id == s.id, Stream.draft_version == claimed, Stream.push_state == "pushing")
            .values(push_state="idle", push_started_at=None, draft_version=claimed + 1)
        )
        if res.rowcount != 1:
            self.db.rollback()
            self.db.refresh(s)
            raise ServiceError(409, "push_superseded",
                               "Пока шла публикация, поток был пересинхронизирован. Сделайте Fetch, чтобы увидеть фактическое состояние.",
                               stream=self.stream_view(s))
        self.db.refresh(s)
        try:
            L.mark_published(s.links, after.offers)
        except EditorError as e:
            self.db.rollback()
            self._release(s, claimed, e.message)
            raise ServiceError(502, "push_unconfirmed", e.message, stream=self.stream_view(s)) from e
        s.remote_snapshot = after.raw
        s.synced_at = _now()
        changed = changed_protected_fields(baseline, after.raw)
        s.push_baseline = None
        warning = None
        if changed:
            result = "side_effect_detected"
            warning = ("Офферы опубликованы, НО в Keitaro изменились защищённые поля потока: " + ", ".join(changed)
                       + ". Остановите прогон и проверьте поток в Keitaro. Push этого потока заблокирован до Fetch.")
            s.side_effect_fields = changed
            s.last_error = warning
        else:
            s.side_effect_fields = None
            s.last_error = None
        self.db.commit()
        return {"stream": self.stream_view(s), "result": result, "warning": warning, "changed_fields": changed}
