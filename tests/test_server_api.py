import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server


class FakeProcess:
    _next_pid = 40000

    def __init__(self, cmd):
        self.cmd = cmd
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self._running = True
        self.returncode = None
        self.stdout = io.StringIO("")

    def poll(self):
        return None if self._running else self.returncode

    def terminate(self):
        self._running = False
        if self.returncode is None:
            self.returncode = 143

    def finish(self, returncode=0, output=""):
        self._running = False
        self.returncode = returncode
        self.stdout = io.StringIO(output)


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(server, "PROFILE_PATH", tmp_path / "profile.json")
    monkeypatch.setattr(server, "SEARCHES_PATH", tmp_path / "searches.yaml")
    monkeypatch.setattr(server, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(server, "RESUME_PATH", tmp_path / "resume.txt")
    monkeypatch.setattr(server, "LOGS_DIR", tmp_path / "logs")

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(server, "JOBSPY_PATH", root / "applypilot" / "src" / "applypilot" / "discovery" / "jobspy.py")
    monkeypatch.setattr(server, "PIPELINE_PATH", root / "applypilot" / "src" / "applypilot" / "pipeline.py")

    server._pipeline_proc = None
    server._pipeline_meta = {
        "stages": None,
        "resolved_stages": None,
        "min_score": None,
        "workers": None,
        "dry_run": None,
        "command": None,
        "started_at": None,
        "finished_at": None,
        "returncode": None,
        "output": "",
        "output_lines": [],
        "output_captured": False,
    }
    return server


@pytest.fixture()
def client(srv):
    return TestClient(srv.app)


@pytest.fixture()
def popen_spy(monkeypatch, srv):
    calls = []

    def fake_popen(cmd, *args, **kwargs):
        proc = FakeProcess(cmd)
        calls.append(proc)
        return proc

    monkeypatch.setattr(srv.subprocess, "Popen", fake_popen)
    return calls


def seed_jobs_db(srv, tmp_path):
    srv._initialize_jobs_db()
    tailored = tmp_path / "tailored.txt"
    tailored.write_text("TAILORED")
    cover = tmp_path / "cover.txt"
    cover.write_text("COVER")

    conn = sqlite3.connect(srv.DB_PATH)
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, salary, location, site, strategy, discovered_at,
            description, full_description, application_url, detail_error,
            fit_score, score_reasoning, scored_at,
            tailored_resume_path, tailored_at,
            cover_letter_path, cover_letter_at,
            applied_at, apply_status, apply_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "https://example.com/job-1",
            "Backend Engineer",
            "$150k",
            "Remote",
            "indeed",
            "jobspy",
            "2026-02-20T00:00:00+00:00",
            "desc 1",
            "full desc 1",
            "https://apply.example.com/1",
            None,
            9,
            "python,fastapi\nstrong match",
            "2026-02-20T00:01:00+00:00",
            str(tailored),
            "2026-02-20T00:02:00+00:00",
            str(cover),
            "2026-02-20T00:03:00+00:00",
            None,
            None,
            None,
        ),
    )
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, salary, location, site, strategy, discovered_at,
            description, full_description, application_url, detail_error,
            fit_score, score_reasoning, scored_at,
            tailored_resume_path, tailored_at,
            cover_letter_path, cover_letter_at,
            applied_at, apply_status, apply_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "https://example.com/job-2",
            "Frontend Engineer",
            "$130k",
            "Austin, TX",
            "linkedin",
            "jobspy",
            "2026-02-20T00:10:00+00:00",
            "desc 2",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "2026-02-20T00:20:00+00:00",
            "applied",
            None,
        ),
    )
    conn.commit()
    conn.close()


def test_root_serves_ui(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_stats_without_db_returns_zeroes(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["sources"] == {}


def test_stats_with_db(client, srv, tmp_path):
    seed_jobs_db(srv, tmp_path)
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["scored"] == 1
    assert data["scored_7plus"] == 1
    assert data["applied"] == 1


def test_jobs_without_db_returns_empty(client):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": [], "total": 0}


def test_jobs_with_db_has_expected_fields(client, srv, tmp_path):
    seed_jobs_db(srv, tmp_path)
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["jobs"]) == 2
    job = body["jobs"][0]
    assert set(["title", "company", "status", "url", "fit_score"]).issubset(job.keys())


def test_job_detail_found_includes_file_text(client, srv, tmp_path):
    seed_jobs_db(srv, tmp_path)
    resp = client.get("/api/jobs/detail", params={"url": "https://example.com/job-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://example.com/job-1"
    assert body["tailored_resume_text"] == "TAILORED"
    assert body["cover_letter_text"] == "COVER"


def test_job_detail_not_found(client, srv, tmp_path):
    seed_jobs_db(srv, tmp_path)
    resp = client.get("/api/jobs/detail", params={"url": "https://missing.example.com"})
    assert resp.status_code == 404


def test_boards_endpoint(client):
    resp = client.get("/api/boards")
    assert resp.status_code == 200
    data = resp.json()
    assert "boards" in data
    assert "manual_ats" in data
    assert "blocked_sso" in data
    names = {b["name"].lower(): b for b in data["boards"]}
    assert "glassdoor" in names
    assert names["glassdoor"]["blocked"] is True


def test_config_defaults_endpoint(client):
    resp = client.get("/api/config/defaults")
    assert resp.status_code == 200
    body = resp.json()
    assert "defaults" in body
    assert body["defaults"]["min_score"] == 7
    assert "pipeline_stages" in body
    assert "apply" in body["pipeline_stages"]
    assert "tier_labels" in body


def test_profile_get_default_and_put(client, srv):
    get_resp = client.get("/api/config/profile")
    assert get_resp.status_code == 200
    assert get_resp.json()["min_score"] == 7

    payload = {
        "min_score": 8,
        "personal": {"full_name": "Mike"},
        "experience": {},
        "compensation": {},
        "skills_boundary": {},
    }
    put_resp = client.put("/api/config/profile", json=payload)
    assert put_resp.status_code == 200
    assert put_resp.json()["min_score"] == 8

    saved = json.loads(srv.PROFILE_PATH.read_text())
    assert saved["min_score"] == 8


def test_searches_get_default_and_put_syncs_boards_sites(client, srv):
    get_resp = client.get("/api/config/searches")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert isinstance(body["boards"], list)

    put_resp = client.put("/api/config/searches", json={"boards": ["indeed", "linkedin"]})
    assert put_resp.status_code == 200
    saved = yaml_safe_load(srv.SEARCHES_PATH.read_text())
    assert saved["boards"] == ["indeed", "linkedin"]
    assert saved["sites"] == ["indeed", "linkedin"]


def yaml_safe_load(text):
    import yaml

    return yaml.safe_load(text)


def test_resume_get_and_put(client, srv):
    assert client.get("/api/config/resume").json() == {"text": ""}

    put_resp = client.put("/api/config/resume", json={"text": "hello resume"})
    assert put_resp.status_code == 200
    assert put_resp.json()["chars"] == 12
    assert srv.RESUME_PATH.read_text() == "hello resume"


def test_capsolver_get_and_put(client, srv):
    first = client.get("/api/config/capsolver")
    assert first.status_code == 200
    assert first.json()["configured"] is False

    put_resp = client.put("/api/config/capsolver", json={"key": "CAP-TEST-1234"})
    assert put_resp.status_code == 200
    assert put_resp.json()["configured"] is True
    assert put_resp.json()["key_hint"].endswith("1234")

    second = client.get("/api/config/capsolver")
    assert second.json()["configured"] is True


def test_env_get_and_put_and_clear(client):
    put_resp = client.put("/api/config/env", json={"CAPSOLVER_API_KEY": "CAP-XYZ-9999"})
    assert put_resp.status_code == 200
    assert put_resp.json()["capsolver_configured"] is True

    get_resp = client.get("/api/config/env")
    assert get_resp.status_code == 200
    assert get_resp.json()["CAPSOLVER_API_KEY"] == "CAP-XYZ-9999"

    clear_resp = client.put("/api/config/env", json={"CAPSOLVER_API_KEY": ""})
    assert clear_resp.status_code == 200
    assert clear_resp.json()["capsolver_configured"] is False


def test_jobs_export_csv(client, srv, tmp_path):
    seed_jobs_db(srv, tmp_path)
    resp = client.get("/api/jobs/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    assert "URL,Title" in resp.text


def test_pipeline_run_form_body_reads_stages(client, popen_spy):
    resp = client.post(
        "/api/pipeline/run",
        data={"stages": "score", "min_score": "7", "workers": "2", "dry_run": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_stages"] == "score"
    assert body["workers"] == 2
    assert body["dry_run"] is True
    assert " run score " in body["command"]
    assert popen_spy


def test_pipeline_run_json_and_alias(client, popen_spy):
    resp = client.post(
        "/api/pipeline/run",
        json={"stages": "cover_letter", "min_score": 8, "workers": 1, "dry_run": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_stages"] == "cover"
    assert body["min_score"] == 8


def test_pipeline_run_rejects_invalid_workers(client):
    resp = client.post("/api/pipeline/run", data={"stages": "score", "workers": "0"})
    assert resp.status_code == 400
    assert "workers must be between" in resp.json()["detail"]


def test_pipeline_run_already_running(client, srv):
    srv._pipeline_proc = FakeProcess(["dummy"])
    resp = client.post("/api/pipeline/run", data={"stages": "score"})
    assert resp.status_code == 200
    assert resp.json()["error"] == "Pipeline already running"


def test_pipeline_status_and_stop(client, popen_spy, srv):
    idle = client.get("/api/pipeline/status")
    assert idle.status_code == 200
    assert idle.json()["running"] is False

    run = client.post("/api/pipeline/run", data={"stages": "score"})
    assert run.status_code == 200
    running = client.get("/api/pipeline/status")
    assert running.status_code == 200
    assert running.json()["running"] is True

    proc = popen_spy[-1]
    proc.finish(returncode=0, output="done output")
    complete = client.get("/api/pipeline/status")
    assert complete.status_code == 200
    assert complete.json()["running"] is False
    assert "done output" in complete.json()["output"]

    # restart then stop
    client.post("/api/pipeline/run", data={"stages": "score"})
    stopped = client.post("/api/pipeline/stop")
    assert stopped.status_code == 200
    assert stopped.json()["ok"] is True


def test_logs_endpoint_includes_pipeline_and_files(client, srv):
    srv._pipeline_meta = {
        "stages": "score",
        "resolved_stages": "score",
        "started_at": "2026-02-20T00:00:00+00:00",
        "finished_at": "2026-02-20T00:02:00+00:00",
        "returncode": 0,
        "output": "line1\nline2\nline3",
        "output_lines": ["line1", "line2", "line3"],
        "output_captured": True,
    }
    srv.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (srv.LOGS_DIR / "server.log").write_text(
        "2026-02-20T00:00:00+00:00 startup\nerror: bad thing happened\ntrace line\n"
    )

    resp = client.get("/api/logs?tail=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tail"] == 2
    assert body["pipeline"]["total_lines"] == 3
    assert [line["text"] for line in body["pipeline"]["lines"]] == ["line2", "line3"]
    assert body["log_files"]
    file_payload = next(f for f in body["log_files"] if f["name"] == "server.log")
    assert file_payload["total_lines"] == 3
    assert [line["text"] for line in file_payload["lines"]] == ["error: bad thing happened", "trace line"]


def test_logs_stream_since_cursor(client, srv):
    srv._pipeline_meta = {
        "stages": "score",
        "resolved_stages": "score",
        "started_at": "2026-02-20T00:00:00+00:00",
        "finished_at": "2026-02-20T00:02:00+00:00",
        "returncode": 0,
        "output": "a\nb\nc\nd",
        "output_lines": ["a", "b", "c", "d"],
        "output_captured": True,
    }

    resp = client.get("/api/logs/stream?since=2&tail=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["since"] == 2
    assert body["next_since"] == 4
    assert [line["text"] for line in body["lines"]] == ["c", "d"]


def test_system_check_and_alias(client, monkeypatch, srv):
    monkeypatch.setattr(srv.shutil, "which", lambda name: "/usr/bin/gemini" if name == "gemini" else None)
    resp = client.get("/api/system/check")
    assert resp.status_code == 200
    checks = resp.json()["checks"]
    gemini = next(c for c in checks if c["name"] == "Gemini CLI")
    assert gemini["ok"] is True

    alias = client.get("/api/system/checks")
    assert alias.status_code == 200
    assert "checks" in alias.json()


def test_system_open_config(client, monkeypatch, srv):
    calls = []

    def fake_popen(cmd, *args, **kwargs):
        calls.append(cmd)
        return FakeProcess(cmd)

    monkeypatch.setattr(srv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(srv.sys, "platform", "darwin")

    resp = client.post("/api/system/open-config")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls


def test_system_reset_database_success(client, srv):
    srv._initialize_jobs_db()
    (Path(str(srv.DB_PATH) + "-wal")).write_text("wal")
    (Path(str(srv.DB_PATH) + "-shm")).write_text("shm")

    resp = client.post("/api/system/reset-database")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert Path(body["path"]).exists()


def test_system_reset_database_conflict_when_pipeline_running(client, srv):
    srv._pipeline_proc = FakeProcess(["dummy"])
    resp = client.post("/api/system/reset-database")
    assert resp.status_code == 409
