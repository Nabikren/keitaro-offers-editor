import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.devtools.fake_keitaro import API_KEY, FakeKeitaro
from app.keitaro.client import KeitaroClient
from app.main import create_app


@pytest.fixture
def fake() -> FakeKeitaro:
    return FakeKeitaro().seed_demo()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        keitaro_url="https://kt.test",
        keitaro_api_key=API_KEY,
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        campaign_name_prefix="test_simonov_",
        redirect_url="https://www.google.com/",
    )


@pytest.fixture
def api(settings, fake):
    # Схема создаётся миграциями — так тесты заодно проверяют Alembic с нуля.
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    app = create_app(settings, kt_factory=lambda: KeitaroClient(settings.keitaro_url, settings.keitaro_api_key, 2, transport=fake.transport()))
    with TestClient(app) as client:
        yield client
