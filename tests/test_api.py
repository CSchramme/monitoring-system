from app.service import record_result
from app.checks import CheckResult


def test_setup_login_logout(client):
    status = client.get("/api/auth/status").json()
    assert status["setup_required"] is True and status["user"] is None
    assert client.get("/api/monitors").status_code == 401

    assert client.post("/api/auth/setup", json={"username": "admin", "password": "short"}).status_code == 422
    assert client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"}).status_code == 200
    assert client.get("/api/auth/status").json()["user"]["name"] == "admin"
    # setup only works once
    assert client.post("/api/auth/setup", json={"username": "x", "password": "supersecret"}).status_code == 409

    client.post("/api/auth/logout")
    assert client.get("/api/monitors").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "supersecret"}).status_code == 200
    assert client.get("/api/monitors").status_code == 200


def test_login_throttle(client):
    client.post("/api/auth/setup", json={"username": "admin", "password": "supersecret"})
    client.post("/api/auth/logout")
    for _ in range(10):
        client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert client.post("/api/auth/login", json={"username": "admin", "password": "supersecret"}).status_code == 429


def test_change_password(auth_client):
    r = auth_client.post("/api/auth/password", json={"current_password": "bad", "new_password": "newsecret1"})
    assert r.status_code == 400
    r = auth_client.post("/api/auth/password", json={"current_password": "supersecret", "new_password": "newsecret1"})
    assert r.status_code == 200
    auth_client.post("/api/auth/logout")
    assert auth_client.post("/api/auth/login", json={"username": "admin", "password": "newsecret1"}).status_code == 200


def test_monitor_crud_and_validation(auth_client):
    bad = auth_client.post("/api/monitors", json={"name": "x", "type": "http", "target": "example.com"})
    assert bad.status_code == 422
    bad = auth_client.post("/api/monitors", json={"name": "x", "type": "tcp", "target": "example.com"})
    assert bad.status_code == 422
    bad = auth_client.post("/api/monitors", json={"name": "x", "type": "ping", "target": "-f host"})
    assert bad.status_code == 422
    bad = auth_client.post(
        "/api/monitors",
        json={"name": "x", "type": "http", "target": "https://a.b", "config": {"expected_status": "abc"}},
    )
    assert bad.status_code == 422

    created = auth_client.post(
        "/api/monitors",
        json={"name": "Website", "type": "http", "target": "https://example.com", "group_name": "Web",
              "config": {"keyword": "Example", "unknown_key": 1}},
    )
    assert created.status_code == 201
    monitor = created.json()
    assert monitor["status"] == "pending" and monitor["push_path"] is None
    assert monitor["config"]["keyword"] == "Example" and "unknown_key" not in monitor["config"]

    listed = auth_client.get("/api/monitors").json()
    assert [m["name"] for m in listed] == ["Website"]

    updated = auth_client.put(
        f"/api/monitors/{monitor['id']}",
        json={"name": "Website 2", "type": "http", "target": "https://example.org"},
    ).json()
    assert updated["name"] == "Website 2" and updated["target"] == "https://example.org"

    paused = auth_client.post(f"/api/monitors/{monitor['id']}/pause").json()
    assert paused["status"] == "paused" and paused["enabled"] is False
    resumed = auth_client.post(f"/api/monitors/{monitor['id']}/resume").json()
    assert resumed["status"] == "pending" and resumed["enabled"] is True

    events = auth_client.get("/api/events", params={"monitor_id": monitor["id"]}).json()
    assert [e["status"] for e in events] == ["pending", "paused"]

    assert auth_client.delete(f"/api/monitors/{monitor['id']}").status_code == 204
    assert auth_client.get(f"/api/monitors/{monitor['id']}").status_code == 404


def _create_push(client, **config):
    body = {"name": "Server", "type": "push", "interval": 60, "config": config}
    response = client.post("/api/monitors", json=body)
    assert response.status_code == 201
    return response.json()


def test_push_heartbeat_and_metrics(auth_client):
    monitor = _create_push(auth_client, thresholds=[{"metric": "cpu", "op": ">", "value": 90}])
    path = monitor["push_path"]
    assert path.startswith("/api/push/")

    auth_client.post("/api/auth/logout")  # push endpoints need no login
    assert auth_client.get("/api/push/doesnotexist").status_code == 404

    r = auth_client.get(path, params={"status": "up", "msg": "hi", "ping": "12.5"})
    assert r.status_code == 200 and r.json()["status"] == "up"

    r = auth_client.post(path, json={"message": "web01", "metrics": {"cpu": 95, "mem": 40, "bad name": 1, "s": "x"}})
    assert r.json()["status"] == "down"
    assert r.json()["accepted_metrics"] == ["cpu", "mem"]

    r = auth_client.post(path, json={"status": "up", "metrics": {"cpu": 20}})
    assert r.json()["status"] == "up"

    assert auth_client.post(path, json={"status": "banana"}).status_code == 400
    assert auth_client.post(path, content=b"not json").status_code == 400
    # an empty POST is a plain heartbeat
    assert auth_client.post(path).json()["status"] == "up"

    auth_client.post("/api/auth/login", json={"username": "admin", "password": "supersecret"})
    detail = auth_client.get(f"/api/monitors/{monitor['id']}").json()
    assert detail["last_metrics"] == {"cpu": 20.0}
    assert detail["stats"]["uptime_24h"] == 75.0

    results = auth_client.get(f"/api/monitors/{monitor['id']}/results", params={"range": "1h"}).json()
    assert len(results["points"]) == 4
    assert results["metric_keys"] == ["cpu", "mem"]

    events = auth_client.get("/api/events").json()
    assert [e["status"] for e in events] == ["up", "down", "up"]

    old_path = path
    regenerated = auth_client.post(f"/api/monitors/{monitor['id']}/token").json()
    assert regenerated["push_path"] != old_path
    assert auth_client.get(old_path).status_code == 404


def test_retries_delay_down(app, auth_client):
    monitor = auth_client.post(
        "/api/monitors", json={"name": "db", "type": "tcp", "target": "127.0.0.1:1", "retries": 2}
    ).json()
    db = app.state.db
    record_result(db, monitor["id"], CheckResult(True, 1.0, "ok"))
    for expected in ("up", "up", "down"):
        record_result(db, monitor["id"], CheckResult(False, None, "refused"))
        assert db.scalar("SELECT status FROM monitors WHERE id = ?", (monitor["id"],)) == expected


def test_check_now(auth_client):
    monitor = auth_client.post(
        "/api/monitors", json={"name": "closed port", "type": "tcp", "target": "127.0.0.1:1", "timeout": 2}
    ).json()
    r = auth_client.post(f"/api/monitors/{monitor['id']}/check")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert auth_client.get(f"/api/monitors/{monitor['id']}").json()["status"] == "down"

    push = _create_push(auth_client)
    assert auth_client.post(f"/api/monitors/{push['id']}/check").status_code == 400


def test_api_key_auth_and_prometheus(auth_client):
    created = auth_client.post("/api/keys", json={"name": "grafana"}).json()
    key = created["key"]
    assert key.startswith("mon_")
    monitor = _create_push(auth_client)
    auth_client.post(monitor["push_path"], json={"metrics": {"cpu": 12.5}})
    auth_client.post("/api/auth/logout")

    assert auth_client.get("/metrics").status_code == 401
    assert auth_client.get("/metrics", headers={"Authorization": "Bearer mon_wrong"}).status_code == 401
    response = auth_client.get("/metrics", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert 'monitor_up{id="%d",name="Server",type="push",group=""} 1' % monitor["id"] in response.text
    assert 'metric="cpu"} 12.5' in response.text

    listed = auth_client.get("/api/keys", headers={"Authorization": f"Bearer {key}"}).json()
    assert listed[0]["name"] == "grafana" and "key_hash" not in listed[0] and listed[0]["last_used_at"]

    assert auth_client.delete(f"/api/keys/{created['id']}", headers={"Authorization": f"Bearer {key}"}).status_code == 204
    assert auth_client.get("/metrics", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_channels(auth_client):
    assert auth_client.post("/api/channels", json={"name": "x", "type": "discord", "config": {}}).status_code == 422
    assert auth_client.post(
        "/api/channels", json={"name": "x", "type": "webhook", "config": {"url": "ftp://x"}}
    ).status_code == 422
    channel = auth_client.post(
        "/api/channels", json={"name": "Hook", "type": "webhook", "config": {"url": "http://127.0.0.1:1/hook"}}
    ).json()
    email = auth_client.post(
        "/api/channels",
        json={"name": "Mail", "type": "email",
              "config": {"host": "smtp.test", "sender": "a@b.c", "recipients": "x@y.z, q@r.s"}},
    ).json()
    assert email["config"]["recipients"] == ["x@y.z", "q@r.s"]

    monitor = auth_client.post(
        "/api/monitors",
        json={"name": "n", "type": "push", "channel_ids": [channel["id"], email["id"]]},
    ).json()
    assert sorted(monitor["channel_ids"]) == sorted([channel["id"], email["id"]])
    assert auth_client.post("/api/monitors", json={"name": "n", "type": "push", "channel_ids": [999]}).status_code == 422

    # sending to an unreachable webhook reports the error and stores it on the channel
    r = auth_client.post(f"/api/channels/{channel['id']}/test")
    assert r.status_code == 502
    stored = [c for c in auth_client.get("/api/channels").json() if c["id"] == channel["id"]][0]
    assert stored["last_error"]

    assert auth_client.delete(f"/api/channels/{channel['id']}").status_code == 204
    assert auth_client.get(f"/api/monitors/{monitor['id']}").json()["channel_ids"] == [email["id"]]


def test_ui_and_agent_served(client):
    index = client.get("/")
    assert index.status_code == 200 and "<html" in index.text.lower()
    assert index.headers["cache-control"] == "no-cache"
    agent = client.get("/agent/linux-agent.sh")
    assert agent.status_code == 200 and agent.text.startswith("#!")


def test_password_hash_is_php_compatible_bcrypt(app, auth_client):
    stored = app.state.users.get_user_by_name("admin")["password_hash"]
    assert stored.startswith("$2y$12$")
