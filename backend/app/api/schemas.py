"""Тела HTTP-запросов API."""

from pydantic import BaseModel, Field


class VersionBody(BaseModel):
    expected_version: int


class AddOfferBody(VersionBody):
    offer_id: int = Field(gt=0)


class PinBody(VersionBody):
    share: int | None = Field(default=None, ge=1, le=100)


class FetchBody(BaseModel):
    discard_drafts: bool = False


class CreateCampaignBody(BaseModel):
    idempotency_key: str
    name: str
    geo: str
    offer_id: int | None = None
    domain_id: int | None = None
    group_id: int | None = None
    traffic_source_id: int | None = None
