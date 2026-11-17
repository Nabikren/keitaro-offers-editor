"""Офлайн-сверка зависших и неподтверждённых Push. Запускать ТОЛЬКО при остановленном backend:

    cd backend
    python -m app.recover

Что делает для каждого потока с push_state="pushing" или с сохранённым push_baseline:
- читает поток в Keitaro (GET /streams/{id}); записей в Keitaro не делает никогда —
  клиент сверки умеет только читать;
- сравнивает защищённые поля со снимком перед PUT (если он есть) и фиксирует побочный эффект;
- офферы уже совпадают с черновиком → отмечает их опубликованными;
  совпадают с прежним опубликованным → запись не применилась, черновик остаётся;
  иначе → Keitaro изменён вне редактора, черновик остаётся, нужен Fetch;
- 404 → поток удалён, помечается remote_missing;
- снимает push_state="pushing" и push_baseline, увеличивает версию.
Любая ошибка чтения (кроме 404) оставляет поток как есть — повторите сверку позже.
"""

import sys
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import Database
from .domain import links as L
from .domain.links import EditorError
from .keitaro.client import KeitaroClient, KeitaroError
from .keitaro.schemas import changed_protected_fields, parse_stream
from .models import Stream
from .runlock import LockBusy, RunLock, lock_path_for


class ReadOnlyKeitaro:
    """Обёртка, через которую физически нельзя записать в Keitaro: доступно только чтение потока."""

    def __init__(self, client: KeitaroClient):
        self._client = client

    def get_stream(self, stream_id: int) -> dict:
        return self._client.get_stream(stream_id)


def recover_streams(db: Session, kt: ReadOnlyKeitaro) -> list[dict]:
    report = []
    rows = list(db.scalars(select(Stream).where(or_(Stream.push_state == "pushing", Stream.push_baseline.is_not(None)))))
    for s in rows:
        if s.push_state != "pushing" and s.push_baseline is None:
            continue  # JSON null, сохранённый как значение, а не SQL NULL
        item = {"stream_id": s.id, "was_pushing": s.push_state == "pushing", "had_baseline": s.push_baseline is not None}
        try:
            raw = kt.get_stream(s.id)
        except KeitaroError as e:
            if e.kind != "not_found":
                report.append({**item, "outcome": "error", "message": e.message})
                continue
            s.remote_missing = True
            s.side_effect_fields = s.side_effect_fields or None
            s.last_error = "Офлайн-сверка: поток удалён в Keitaro."
            outcome, drift = "missing", []
        else:
            try:
                view = parse_stream(raw)
            except KeitaroError as e:
                report.append({**item, "outcome": "error", "message": e.message})
                continue
            drift = changed_protected_fields(s.push_baseline, raw) if s.push_baseline is not None else []
            if L.desired_matches(s.links, view.offers):
                try:
                    L.mark_published(s.links, view.offers)
                except EditorError as e:
                    report.append({**item, "outcome": "error", "message": e.message})
                    continue
                outcome = "applied"
                message = "Офлайн-сверка: офферы подтверждены чтением из Keitaro."
            elif L.published_matches(s.links, view.offers):
                outcome = "not_applied"
                message = "Офлайн-сверка: запись не применилась, черновик сохранён — Push можно повторить."
            else:
                outcome = "remote_changed"
                message = "Офлайн-сверка: состав офферов в Keitaro изменён вне редактора — сделайте Fetch."
            if drift:
                message += " Изменились защищённые поля: " + ", ".join(drift) + ". Проверьте поток в Keitaro."
            s.remote_snapshot = raw
            s.side_effect_fields = drift or s.side_effect_fields or None
            s.last_error = message
        s.push_state, s.push_started_at, s.push_baseline = "idle", None, None
        s.draft_version += 1
        s.synced_at = datetime.now(timezone.utc)
        db.commit()
        report.append({**item, "outcome": outcome, "side_effect_fields": drift})
    return report


def run(settings: Settings, kt: ReadOnlyKeitaro) -> list[dict]:
    """Захватывает блокировку базы (backend должен быть остановлен) и выполняет сверку."""
    lock = RunLock(lock_path_for(settings.database_url))
    lock.acquire("Офлайн-сверка")
    try:
        db = Database(settings.database_url)
        with db.sessions() as session:
            return recover_streams(session, kt)
    finally:
        lock.release()


def main() -> int:
    settings = get_settings()
    if not settings.keitaro_configured:
        print("Keitaro не настроен: задайте KEITARO_URL и KEITARO_API_KEY", file=sys.stderr)
        return 2
    client = KeitaroClient(settings.keitaro_url, settings.keitaro_api_key, settings.keitaro_timeout_seconds)
    try:
        report = run(settings, ReadOnlyKeitaro(client))
    except LockBusy as e:
        print(f"{e} Остановите backend и повторите.", file=sys.stderr)
        return 3
    finally:
        client.close()
    if not report:
        print("Потоков, требующих сверки, нет.")
    for item in report:
        print(item)
    return 1 if any(i["outcome"] == "error" for i in report) else 0


if __name__ == "__main__":
    sys.exit(main())
