"""Dashboard: flow graph, command box to the Tech Lead, and adding or reconfiguring projects from the web."""
from __future__ import annotations

import json

import yaml

from agentbridge.demo import create_demo_repo

from .conftest import q, run
from .test_security import H, client  # noqa: F401  (fixture)


def test_graph_api_and_page(client):  # noqa: F811
    c, e = client
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "G1")
    run(e, 120)
    g = c.get("/api/projects/demo-calc/graph", headers=H).json()
    ids = [n["id"] for n in g["nodes"]]
    assert ids[:2] == ["goal", "lead"] and "integ" in ids and g["milestone"]["key"] == "G1"
    tasks = {n["title"]: n for n in g["nodes"] if n["type"] == "task"}
    assert set(tasks) == {"fix-add", "impl-mul"} and all(n["bucket"] == "done" for n in tasks.values())
    edges = {(x["from"], x["to"]) for x in g["edges"]}
    fa, im = tasks["fix-add"]["id"], tasks["impl-mul"]["id"]
    assert {("goal", "lead"), ("lead", fa), (fa, im), (im, "integ")} <= edges
    assert tasks["impl-mul"]["col"] > tasks["fix-add"]["col"]
    r = c.get(f"/projects/demo-calc/graph?m={mid}", headers=H)
    assert r.status_code == 200 and 'id="flow"' in r.text and "Chỉ đạo Tech Lead" in r.text
    assert "Tạo task" in r.text                                   # the Tech Lead's decisions in the thread
    assert c.get("/static/graph.js").status_code == 200 and c.get("/static/nope.js").status_code == 404
    assert c.get("/api/projects/demo-calc/graph").status_code in (303, 307, 401)   # data needs a session


def test_command_box_goal_and_instruction(client):  # noqa: F811
    c, e = client
    r = c.post("/projects/demo-calc/command", headers=H, data={"mode": "note", "text": "Prefer small commits"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?sent=stored")      # nothing running yet
    r = c.post("/projects/demo-calc/command", headers=H, follow_redirects=False,
               data={"mode": "goal", "text": "Implement mul()", "milestone": "C1", "back": "/projects/demo-calc"})
    assert r.headers["location"] == "/projects/demo-calc?sent=goal"
    assert q(e, "SELECT key, status FROM milestones") == [{"key": "C1", "status": "ACTIVE"}]
    r = c.post("/projects/demo-calc/command", headers=H, data={"text": "Also add a docstring"}, follow_redirects=False)
    assert r.headers["location"].endswith("?sent=note")
    assert q(e, "SELECT COUNT(*) n FROM events WHERE type='HUMAN_DECISION'")[0]["n"] == 1
    assert "Prefer small commits" in c.get("/projects/demo-calc", headers=H).text
    r = c.post("/projects/demo-calc/command", headers=H, data={"text": "go", "back": "https://evil.example"},
               follow_redirects=False)
    assert r.status_code == 400


def test_add_project_from_the_web(client, tmp_path):  # noqa: F811
    c, e = client
    assert "+ Thêm dự án" in c.get("/projects", headers=H).text
    form = c.get("/projects/new", headers=H).text
    assert "mock-lead" in form and "mock-reviewer" in form
    repo = create_demo_repo(tmp_path / "second")
    base = {"git_url": str(repo), "slug": "second-app", "name": "Second app", "base_branch": "main",
            "tech_lead": "mock-lead", "worker": "mock-worker", "reviewers": ["mock-reviewer"],
            "review_policy": "risk", "test_cmd": "python3 -m unittest discover -s tests -t .",
            "charter": "Small and tested.", "first_goal": "Fix add() and implement mul()"}
    bad = c.post("/projects/new", headers=H, data=base | {"tech_lead": "nobody"})
    assert bad.status_code == 200 and "agent không tồn tại: nobody" in bad.text
    bad = c.post("/projects/new", headers=H, data=base | {"test_cmd": "curl http://x"})
    assert "lệnh test không được phép: curl" in bad.text
    assert q(e, "SELECT COUNT(*) n FROM project_requests")[0]["n"] == 0
    r = c.post("/projects/new", headers=H, data=base, follow_redirects=False)
    assert r.status_code == 303
    run(e, 120)
    req = q(e, "SELECT status, error FROM project_requests")[0]
    assert req == {"status": "DONE", "error": None}
    p = q(e, "SELECT * FROM projects WHERE slug='second-app'")[0]
    m = json.loads(p["manifest_json"])
    assert p["repo_path"] == str(repo) and m["validation"]["default"] == ["test"]
    assert m["policy"]["autonomous"] is False and m["project"]["reviewers"] == ["mock-reviewer"]
    ms = q(e, "SELECT status FROM milestones WHERE project_id=?", p["id"])
    assert ms == [{"status": "COMPLETED"}]                      # the first goal ran to the end
    assert c.post("/projects/new", headers=H, data=base).text.count("đã có dự án second-app") == 1


def test_bad_git_url_fails_the_request(client):  # noqa: F811
    c, e = client
    c.post("/projects/new", headers=H, data={"git_url": "ftp://x/repo", "slug": "other", "name": "Other",
                                             "tech_lead": "mock-lead", "worker": "mock-worker"})
    run(e, 30)
    r = q(e, "SELECT status, error FROM project_requests")[0]
    assert r["status"] == "FAILED" and "https://" in r["error"]
    assert "lỗi" in c.get("/projects", headers=H).text


def test_edit_project_config_from_the_web(client):  # noqa: F811
    c, e = client
    page = c.get("/projects/demo-calc", headers=H).text
    assert "Lưu cấu hình" in page and "repo_path" not in page.split("Cấu hình dự án")[1].split("</textarea>")[0]
    data = json.loads(q(e, "SELECT manifest_json FROM projects")[0]["manifest_json"])
    data["project"].pop("repo_path")
    data["project"]["review_policy"] = "always"
    data["project"]["repo_path"] = "/etc"                       # ignored: the repo never moves
    assert c.post("/projects/demo-calc/config", headers=H, data={"yaml": "project: [1"}).status_code == 400
    r = c.post("/projects/demo-calc/config", headers=H, data={"yaml": yaml.safe_dump(data)}, follow_redirects=False)
    assert r.status_code == 303
    run(e, 30)
    p = q(e, "SELECT * FROM projects")[0]
    assert json.loads(p["manifest_json"])["project"]["review_policy"] == "always" and p["repo_path"] != "/etc"
