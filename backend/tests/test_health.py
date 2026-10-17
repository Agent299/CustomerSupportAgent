from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_client(**overrides: object) -> TestClient:
    settings = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def test_healthz_returns_ok() -> None:
    response = make_client().get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_environment() -> None:
    response = make_client(app_env="ci").get("/readyz")

    assert response.json() == {"status": "ready", "env": "ci"}


def test_cors_origins_parsed_from_comma_separated_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CORS_ORIGINS", "http://a.test, https://b.test")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.cors_origins == ["http://a.test", "https://b.test"]
