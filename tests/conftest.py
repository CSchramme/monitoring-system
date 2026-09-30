import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=tmp_path, public_url="https://monitor.test")
    return create_app(settings, start_scheduler=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    response = client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"})
    assert response.status_code == 200
    return client
