"""Integration tests for the MariaDB user store.

Run against a disposable database, e.g.:
  MONITOR_TEST_MARIADB="127.0.0.1:3306:user:password:database" pytest tests/test_mariadb.py
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import Settings
from app.main import create_app
from app.userstore import MariaDBUserStore, UserStoreError

SPEC = os.environ.get("MONITOR_TEST_MARIADB", "")
pytestmark = pytest.mark.skipif(not SPEC, reason="MONITOR_TEST_MARIADB not set")


def _params():
    host, port, user, password, database = SPEC.split(":", 4)
    return dict(host=host, port=int(port), user=user, password=password, database=database)


@pytest.fixture
def table():
    import pymysql

    name = "test_users"
    conn = pymysql.connect(**_params(), autocommit=True)
    with conn.cursor() as cur:
        for t in (name, "monitor_sessions", "monitor_api_keys"):
            cur.execute(f"DROP TABLE IF EXISTS `{t}`")
    yield name, conn
    conn.close()


def _settings(tmp_path, table_name):
    p = _params()
    return Settings(
        data_dir=tmp_path, user_db_host=p["host"], user_db_port=p["port"], user_db_user=p["user"],
        user_db_password=p["password"], user_db_name=p["database"], user_db_table=table_name,
    )


def test_full_auth_flow(tmp_path, table):
    name, _ = table
    with TestClient(create_app(_settings(tmp_path, name), start_scheduler=False)) as client:
        assert client.get("/api/auth/status").json()["setup_required"] is True
        assert client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"}).status_code == 200
        assert client.post("/api/auth/setup", json={"username": "x", "password": "supersecret"}).status_code == 409
        assert client.get("/api/monitors").status_code == 200

        key = client.post("/api/keys", json={"name": "grafana"}).json()["key"]
        assert client.get("/metrics", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        assert client.get("/api/keys").json()[0]["last_used_at"]

        r = client.post("/api/auth/password", json={"current_password": "supersecret", "new_password": "newsecret1"})
        assert r.status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/monitors").status_code == 401
        assert client.post("/api/auth/login", json={"username": "admin", "password": "newsecret1"}).status_code == 200


def test_existing_shared_table_with_php_hash(tmp_path, table):
    name, conn = table
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE `{name}` (id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(64), "
            "password_hash VARCHAR(255), email VARCHAR(255))"
        )
        # Hash as produced by PHP password_hash('geheim123', PASSWORD_DEFAULT)
        cur.execute(
            f"INSERT INTO `{name}` (username, password_hash, email) VALUES (%s, %s, %s)",
            ("christoph", hash_password("geheim123"), "c@example.com"),
        )
    with TestClient(create_app(_settings(tmp_path, name), start_scheduler=False)) as client:
        assert client.get("/api/auth/status").json()["setup_required"] is False
        assert client.post("/api/auth/login", json={"username": "christoph", "password": "geheim123"}).status_code == 200
        assert client.get("/api/auth/status").json()["user"]["name"] == "christoph"


def test_incompatible_table_is_rejected(table):
    name, conn = table
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE `{name}` (id INT PRIMARY KEY, login VARCHAR(64))")
    with pytest.raises(UserStoreError, match="username, password_hash"):
        MariaDBUserStore(**_params(), user_table=name)


def test_invalid_table_name():
    with pytest.raises(UserStoreError):
        MariaDBUserStore(**_params(), user_table="users; DROP TABLE x")
