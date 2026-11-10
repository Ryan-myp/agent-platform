from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_path=tmp_path / "test.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=ModelCatalog.load(CATALOG_PATH),
        api_keys={},
    )
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))


def project_payload() -> dict[str, str]:
    return {
        "track": "drama",
        "business": "security test",
        "product": "security test",
        "audience": "test audience",
    }


def test_cross_origin_write_is_rejected_without_changing_data(tmp_path):
    client = make_client(tmp_path)
    response = client.post(
        "/api/projects",
        json=project_payload(),
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert client.get("/api/projects").json() == []


def test_same_origin_write_remains_available(tmp_path):
    client = make_client(tmp_path)
    response = client.post(
        "/api/projects",
        json=project_payload(),
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 201
    assert len(client.get("/api/projects").json()) == 1


def test_cross_site_fetch_without_origin_is_rejected(tmp_path):
    client = make_client(tmp_path)
    response = client.post(
        "/api/projects",
        json=project_payload(),
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert client.get("/api/projects").json() == []
