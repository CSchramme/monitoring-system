import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# Optional: run the whole suite a second time with users/rights in MariaDB, e.g.
#   MONITOR_TEST_MARIADB="127.0.0.1:3306:user:password:database" pytest
MARIADB = os.environ.get("MONITOR_TEST_MARIADB", "")
USER_TABLES = ("test_users", "roles", "role_permissions", "user_roles", "monitor_sessions", "monitor_api_keys")


def mariadb_params() -> dict:
    host, port, user, password, database = MARIADB.split(":", 4)
    return dict(host=host, port=int(port), user=user, password=password, database=database)


def reset_mariadb() -> None:
    import pymysql

    conn = pymysql.connect(**mariadb_params(), autocommit=True)
    with conn.cursor() as cur:
        for table in USER_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    conn.close()


@pytest.fixture(params=["sqlite", "mariadb"])
def app(request, tmp_path):
    extra = {}
    if request.param == "mariadb":
        if not MARIADB:
            pytest.skip("MONITOR_TEST_MARIADB not set")
        reset_mariadb()
        p = mariadb_params()
        extra = dict(
            user_db_host=p["host"], user_db_port=p["port"], user_db_user=p["user"],
            user_db_password=p["password"], user_db_name=p["database"], user_db_table="test_users",
        )
    settings = Settings(data_dir=tmp_path, public_url="https://monitor.test", **extra)
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
