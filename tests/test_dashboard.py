"""Web dashboard: pages render with live data, and the hardening for a public (Cloudflare) URL."""
from __future__ import annotations

from .conftest import run
from .test_security import H, TOKEN, client  # noqa: F401  (fixture)


def test_pages_render_progress_quota_and_activity(client):  # noqa: F811
    c, e = client
    e.conn.execute("UPDATE agents SET paused=1 WHERE id IN ('mock-worker','mock-worker-b')")
    e.submit_goal("demo-calc", "Fix add() and implement mul()", "WEB1")
    run(e, 20)
    r = c.get("/", headers=H)
    assert r.status_code == 200
    assert "WEB1" in r.text and "0%" in r.text and "fix-add" in r.text and "Hoạt động gần đây" in r.text
    assert "liveRefresh(20)" in r.text
    for path in ("/projects", "/projects/demo-calc", "/agents", "/approvals", "/settings"):
        assert c.get(path, headers=H).status_code == 200, path
    assert c.get("/", follow_redirects=False).status_code in (303, 307, 401)   # no session, no page


def test_security_headers_and_secure_cookie_behind_proxy(client):  # noqa: F811
    c, e = client
    r = c.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers
    r = c.post("/login", data={"token": TOKEN}, headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert r.status_code == 303 and "Secure" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert "strict-transport-security" in r.headers


def test_login_is_throttled_per_client_ip(client):  # noqa: F811
    c, e = client
    ip = {"cf-connecting-ip": "203.0.113.7"}
    for _ in range(5):
        assert c.post("/login", data={"token": "wrong"}, headers=ip).status_code == 401
    r = c.post("/login", data={"token": TOKEN}, headers=ip, follow_redirects=False)
    assert r.status_code == 429                                   # even the right token waits out the window
    other = c.post("/login", data={"token": TOKEN}, headers={"cf-connecting-ip": "198.51.100.2"},
                   follow_redirects=False)
    assert other.status_code == 303
