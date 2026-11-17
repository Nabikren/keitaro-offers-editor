"""HTTP-слой. Бизнес-логика — в services/ и domain/; здесь только маршруты, DI и маппинг ошибок."""

import logging
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .api.schemas import AddOfferBody, CreateCampaignBody, FetchBody, PinBody, VersionBody
from .config import Settings, get_settings
from .db import Database
from .keitaro.client import KeitaroClient, KeitaroError
from .runlock import RunLock, lock_path_for
from .services import catalog
from .services.campaigns import CampaignCreator
from .services.editor import EditorService
from .services.errors import ServiceError

log = logging.getLogger("keitaro_wrapper")

KEITARO_STATUS = {"auth": 502, "not_found": 404, "bad_request": 422, "config": 503}


def create_app(settings: Settings | None = None, kt_factory: Callable[[], KeitaroClient | None] | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_url)

    def default_kt() -> KeitaroClient | None:
        if not settings.keitaro_configured:
            return None
        return KeitaroClient(settings.keitaro_url, settings.keitaro_api_key, settings.keitaro_timeout_seconds)

    make_kt = kt_factory or default_kt
    run_lock = RunLock(lock_path_for(settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Пока backend работает, офлайн-сверка (python -m app.recover) не может стартовать, и наоборот.
        run_lock.acquire("Backend")
        try:
            yield
        finally:
            run_lock.release()

    app = FastAPI(title="Keitaro offers editor", version="1.0.0", lifespan=lifespan)
    app.state.db = db
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["*"], allow_headers=["*"])

    def get_db() -> Iterator[Session]:
        yield from db.session()

    def get_kt() -> Iterator[KeitaroClient | None]:
        kt = make_kt()
        try:
            yield kt
        finally:
            if kt is not None and kt_factory is None:
                kt.close()

    def editor(s: Session = Depends(get_db), kt=Depends(get_kt)) -> EditorService:
        return EditorService(s, kt, settings)

    def creator(s: Session = Depends(get_db), kt=Depends(get_kt)) -> CampaignCreator:
        return CampaignCreator(s, kt, settings)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, e: ServiceError):
        return JSONResponse(e.to_dict(), status_code=e.status)

    @app.exception_handler(KeitaroError)
    async def keitaro_error(_: Request, e: KeitaroError):
        log.warning("Keitaro error: %s", e)
        return JSONResponse({"code": f"keitaro_{e.kind}", "message": e.message, "upstream_status": e.status},
                            status_code=KEITARO_STATUS.get(e.kind, 502))

    # --- общее ---

    @app.get("/api/health")
    def health():
        return {"ok": True, "keitaro_configured": settings.keitaro_configured}

    @app.get("/api/config")
    def config():
        return {"campaign_name_prefix": settings.campaign_name_prefix, "redirect_url": settings.redirect_url,
                "defaults": {"domain_id": settings.default_domain_id, "group_id": settings.default_group_id,
                             "traffic_source_id": settings.default_traffic_source_id},
                "keitaro_configured": settings.keitaro_configured}

    @app.get("/api/references")
    def references(kt=Depends(get_kt)):
        if kt is None:
            raise ServiceError(503, "keitaro_not_configured", "Keitaro не настроен")
        return catalog.references(kt)

    @app.get("/api/offers")
    def offers(q: str = Query("", max_length=100), limit: int = Query(20, ge=1, le=50), refresh: bool = False,
               s: Session = Depends(get_db), kt=Depends(get_kt)):
        if kt is not None:
            catalog.ensure_offer_cache(s, kt, force=refresh)
        return [catalog.offer_dict(o) for o in catalog.search_offers(s, q, limit)]

    # --- часть A: создание ---

    @app.post("/api/campaigns")
    def create_campaign(body: CreateCampaignBody, c: CampaignCreator = Depends(creator)):
        return c.start(body.idempotency_key, body.model_dump(exclude={"idempotency_key"}))

    @app.post("/api/campaign-jobs/{key}/resume")
    def resume_job(key: str, c: CampaignCreator = Depends(creator)):
        return c.resume(key)

    # --- часть B: редактор ---

    @app.get("/api/campaigns")
    def local_campaigns(e: EditorService = Depends(editor)):
        return e.list_local()

    @app.get("/api/campaigns/{campaign_id}")
    def campaign(campaign_id: int, e: EditorService = Depends(editor)):
        return e.campaign_view(campaign_id)

    @app.post("/api/campaigns/{campaign_id}/fetch")
    def fetch(campaign_id: int, body: FetchBody | None = None, e: EditorService = Depends(editor)):
        return e.fetch(campaign_id, discard_drafts=bool(body and body.discard_drafts))

    @app.post("/api/streams/{stream_id}/offers")
    def add_offer(stream_id: int, body: AddOfferBody, e: EditorService = Depends(editor)):
        return e.edit(stream_id, body.expected_version, "add", offer_id=body.offer_id)

    @app.post("/api/streams/{stream_id}/offers/{offer_id}/{op}")
    def link_op(stream_id: int, offer_id: int, op: str, body: PinBody, e: EditorService = Depends(editor)):
        if op not in ("remove", "restore", "pin", "unpin"):
            raise ServiceError(404, "unknown_op", f"Неизвестное действие {op}")
        return e.edit(stream_id, body.expected_version, op, offer_id=offer_id, share=body.share if op == "pin" else None)

    @app.post("/api/streams/{stream_id}/cancel")
    def cancel(stream_id: int, body: VersionBody, e: EditorService = Depends(editor)):
        return e.edit(stream_id, body.expected_version, "cancel")

    @app.post("/api/streams/{stream_id}/push")
    def push(stream_id: int, body: VersionBody, e: EditorService = Depends(editor)):
        return e.push(stream_id, body.expected_version)

    return app


def app_factory() -> FastAPI:
    """Точка входа uvicorn: `uvicorn app.main:app_factory --factory`."""
    return create_app()
