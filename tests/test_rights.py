from app.userstore import has_permission


def _login(client, username, password):
    client.post("/api/auth/logout")
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _roles(client):
    return {r["name"]: r for r in client.get("/api/roles").json()}


def test_has_permission():
    assert has_permission(frozenset({"*"}), "monitoring.edit")
    assert has_permission(frozenset({"monitoring.*"}), "monitoring.edit")
    assert has_permission(frozenset({"monitoring.view"}), "monitoring.view")
    assert not has_permission(frozenset({"monitoring.view"}), "monitoring.edit")
    assert not has_permission(frozenset({"shop.*"}), "monitoring.view")


def test_first_user_is_admin_with_default_roles(auth_client):
    status = auth_client.get("/api/auth/status").json()
    assert status["user"]["permissions"] == ["*"]
    assert set(_roles(auth_client)) == {"Administrator", "Monitoring-Bearbeiter", "Monitoring-Betrachter"}


def test_viewer_editor_and_no_role(auth_client):
    roles = _roles(auth_client)
    for name, role in (("bob", "Monitoring-Betrachter"), ("eve", "Monitoring-Bearbeiter")):
        r = auth_client.post("/api/users", json={"username": name, "password": "password1", "role_ids": [roles[role]["id"]]})
        assert r.status_code == 201
    assert auth_client.post("/api/users", json={"username": "nobody", "password": "password1"}).status_code == 201
    assert auth_client.post("/api/users", json={"username": "bob", "password": "password1"}).status_code == 409

    push = auth_client.post("/api/monitors", json={"name": "srv", "type": "push"}).json()
    auth_client.post("/api/channels", json={"name": "t", "type": "telegram", "config": {"bot_token": "SECRET", "chat_id": "1"}})

    _login(auth_client, "bob", "password1")
    assert auth_client.get("/api/monitors").status_code == 200
    assert auth_client.get(f"/api/monitors/{push['id']}").json()["push_path"] is None
    assert auth_client.get("/api/channels").json()[0]["config"] == {}
    assert auth_client.post("/api/monitors", json={"name": "x", "type": "push"}).status_code == 403
    assert auth_client.get("/api/users").status_code == 403
    key = auth_client.post("/api/keys", json={"name": "mine"}).json()["key"]

    _login(auth_client, "eve", "password1")
    assert auth_client.get(f"/api/monitors/{push['id']}").json()["push_path"].startswith("/api/push/")
    assert auth_client.post("/api/monitors", json={"name": "x", "type": "push"}).status_code == 201
    assert auth_client.get("/api/keys").json() == []  # only own keys

    _login(auth_client, "nobody", "password1")
    assert auth_client.get("/api/auth/status").json()["user"]["permissions"] == []
    assert auth_client.get("/api/monitors").status_code == 403

    # API keys act with the rights of their owner
    auth_client.post("/api/auth/logout")
    headers = {"Authorization": f"Bearer {key}"}
    assert auth_client.get("/metrics", headers=headers).status_code == 200
    assert auth_client.post("/api/monitors", json={"name": "y", "type": "push"}, headers=headers).status_code == 403


def test_rights_changes_apply_immediately_and_lockout_guard(auth_client):
    roles = _roles(auth_client)
    admin_id = roles["Administrator"]["id"]
    viewer_id = roles["Monitoring-Betrachter"]["id"]
    me = [u for u in auth_client.get("/api/users").json() if u["username"] == "admin"][0]

    # cannot remove the last admin, neither by role change, role edit nor role deletion
    r = auth_client.put(f"/api/users/{me['id']}", json={"username": "admin", "role_ids": [viewer_id]})
    assert r.status_code == 409
    r = auth_client.put(f"/api/roles/{admin_id}", json={"name": "Administrator", "permissions": ["monitoring.view"]})
    assert r.status_code == 409
    assert auth_client.delete(f"/api/roles/{admin_id}").status_code == 409
    assert auth_client.delete(f"/api/users/{me['id']}").status_code == 409
    assert auth_client.get("/api/auth/status").json()["user"]["permissions"] == ["*"]

    # custom role with permissions of another application
    r = auth_client.post("/api/roles", json={"name": "Shop", "permissions": ["shop.orders.view", "monitoring.*"]})
    assert r.status_code == 201
    assert auth_client.post("/api/roles", json={"name": "Bad", "permissions": ["drop table"]}).status_code == 422
    shop_id = r.json()["id"]
    user = auth_client.post("/api/users", json={"username": "carl", "password": "password1", "role_ids": [viewer_id]}).json()

    _login(auth_client, "carl", "password1")
    assert auth_client.post("/api/monitors", json={"name": "x", "type": "push"}).status_code == 403
    _login(auth_client, "admin", "supersecret")
    auth_client.put(f"/api/users/{user['id']}", json={"username": "carl", "role_ids": [shop_id]})
    _login(auth_client, "carl", "password1")
    assert auth_client.post("/api/monitors", json={"name": "x", "type": "push"}).status_code == 201

    _login(auth_client, "admin", "supersecret")
    assert auth_client.delete(f"/api/users/{user['id']}").status_code == 204
    assert _login(auth_client, "carl", "password1").status_code == 401


def test_legacy_sqlite_users_become_admins(tmp_path):
    import time

    from app.db import Database
    from app.userstore import SqliteUserStore

    db = Database(tmp_path / "old.db")
    db.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', ?)", (time.time(),))
    store = SqliteUserStore(db)
    assert store.permissions_for(store.get_user_by_name("old")["id"]) == frozenset({"*"})
