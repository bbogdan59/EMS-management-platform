from __future__ import annotations


def get_csrf(client) -> str:
    resp = client.get("/login")
    assert resp.status_code == 200
    token = client.cookies.get("ems_csrf")
    assert token
    return token


def login(client, email: str, password: str):
    csrf = get_csrf(client)
    resp = client.post(
        "/login", data={"csrf_token": csrf, "email": email, "password": password, "next": "/"}, follow_redirects=False
    )
    return resp
