"""ApplyPilot Web UI Server - FastAPI backend bridging the frontend to ApplyPilot's database and CLI."""

import csv
import datetime as dt
import io
import json
import os
import ast
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, Query
from fastapi import HTTPException
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
try:
    from applypilot.config import (
        DEFAULTS as APPLYPILOT_DEFAULTS,
        ENV_PATH as APPLYPILOT_ENV_PATH,
        SCORE_FILTERS as APPLYPILOT_SCORE_FILTERS,
        TIER_LABELS as APPLYPILOT_TIER_LABELS,
        load_blocked_sites,
        load_blocked_sso,
        load_sites_config,
    )
except ModuleNotFoundError:
    from applypilot.src.applypilot.config import (
        DEFAULTS as APPLYPILOT_DEFAULTS,
        ENV_PATH as APPLYPILOT_ENV_PATH,
        SCORE_FILTERS as APPLYPILOT_SCORE_FILTERS,
        TIER_LABELS as APPLYPILOT_TIER_LABELS,
        load_blocked_sites,
        load_blocked_sso,
        load_sites_config,
    )

app = FastAPI(title="ApplyPilot UI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CONFIG_DIR = Path(APPLYPILOT_ENV_PATH).parent
DB_PATH = CONFIG_DIR / "applypilot.db"
PROFILE_PATH = CONFIG_DIR / "profile.json"
SEARCHES_PATH = CONFIG_DIR / "searches.yaml"
ENV_PATH = Path(APPLYPILOT_ENV_PATH)
RESUME_PATH = CONFIG_DIR / "resume.txt"
VENV_PYTHON = Path(__file__).parent / "applypilot" / ".venv" / "bin" / "python3"
APPLYPILOT_BIN = Path(__file__).parent / "applypilot" / ".venv" / "bin" / "applypilot"
JOBSPY_PATH = Path(__file__).parent / "applypilot" / "src" / "applypilot" / "discovery" / "jobspy.py"
PIPELINE_PATH = Path(__file__).parent / "applypilot" / "src" / "applypilot" / "pipeline.py"

# Track running pipeline processes
_pipeline_proc = None
_pipeline_meta = {
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
    "output_captured": False,
}
DEFAULT_MIN_SCORE = int(APPLYPILOT_DEFAULTS.get("min_score", 7))


def _load_env():
    """Read ~/.applypilot/.env and merge with current os.environ for subprocess use."""
    env = dict(os.environ)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(row):
    return dict(row) if row else None


def _require_db_exists():
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail=f"Database not found: {DB_PATH}")


def _require_file_exists(path: Path, label: str):
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{label} file not found: {path}")


def _ensure_config_dir():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to create config directory {CONFIG_DIR}: {exc}") from exc


def _coerce_min_score(value, *, source: str) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        status = 400 if source == "request" else 500
        raise HTTPException(status_code=status, detail=f"Invalid min_score in {source}: {value!r}") from exc
    if not 1 <= score <= 10:
        status = 400 if source == "request" else 500
        raise HTTPException(status_code=status, detail=f"min_score must be between 1 and 10, got {score}")
    return score


def _coerce_workers(value, *, source: str) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError) as exc:
        status = 400 if source == "request" else 500
        raise HTTPException(status_code=status, detail=f"Invalid workers in {source}: {value!r}") from exc
    if not 1 <= workers <= 64:
        status = 400 if source == "request" else 500
        raise HTTPException(status_code=status, detail=f"workers must be between 1 and 64, got {workers}")
    return workers


def _coerce_bool(value, *, source: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    status = 400 if source == "request" else 500
    raise HTTPException(status_code=status, detail=f"Invalid boolean in {source}: {value!r}")


def _read_env_value(path: Path, key: str) -> Optional[str]:
    if not path.exists():
        return None
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)\s*$")
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed reading env file {path}: {exc}") from exc

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value
    return None


def _upsert_env_value(path: Path, key: str, value: str) -> None:
    _ensure_config_dir()
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text().splitlines()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed reading env file {path}: {exc}") from exc

    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    replaced = False
    out: list[str] = []
    for line in lines:
        if pattern.match(line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")

    payload = "\n".join(out)
    if out:
        payload += "\n"
    try:
        path.write_text(payload)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed writing env file {path}: {exc}") from exc


def _remove_env_key(path: Path, key: str) -> None:
    if not path.exists():
        return
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed reading env file {path}: {exc}") from exc
    kept = [line for line in lines if not pattern.match(line)]
    payload = "\n".join(kept)
    if kept:
        payload += "\n"
    try:
        path.write_text(payload)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed writing env file {path}: {exc}") from exc


def _mask_key_hint(value: Optional[str]) -> str:
    if not value:
        return ""
    return f"****{value[-4:]}"


def _load_jobspy_boards_from_source() -> list[dict]:
    if not JOBSPY_PATH.exists():
        raise HTTPException(status_code=500, detail=f"JobSpy source file not found: {JOBSPY_PATH}")

    try:
        source_text = JOBSPY_PATH.read_text()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed reading JobSpy source file {JOBSPY_PATH}: {exc}") from exc

    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid Python syntax in {JOBSPY_PATH}: {exc}") from exc

    raw = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "JOBSPY_BOARDS":
                    try:
                        raw = ast.literal_eval(node.value)
                    except (SyntaxError, ValueError) as exc:
                        raise HTTPException(
                            status_code=500,
                            detail=f"Invalid JOBSPY_BOARDS constant in {JOBSPY_PATH}: {exc}",
                        ) from exc
                    break
            if raw is not None:
                break
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "JOBSPY_BOARDS":
                try:
                    raw = ast.literal_eval(node.value)
                except (SyntaxError, ValueError) as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Invalid JOBSPY_BOARDS constant in {JOBSPY_PATH}: {exc}",
                    ) from exc
                break

    if raw is None:
        raise HTTPException(status_code=500, detail=f"JOBSPY_BOARDS not found in {JOBSPY_PATH}")
    if not isinstance(raw, list):
        raise HTTPException(status_code=500, detail=f"JOBSPY_BOARDS must be a list in {JOBSPY_PATH}")

    boards: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=500, detail=f"JOBSPY_BOARDS entries must be objects in {JOBSPY_PATH}")
        board_id = str(item.get("id", "")).strip()
        if not board_id:
            raise HTTPException(status_code=500, detail=f"JOBSPY_BOARDS entry missing id in {JOBSPY_PATH}")
        name = str(item.get("name") or board_id).strip()
        board_type = str(item.get("type") or "search").strip()
        boards.append({
            "id": board_id,
            "name": name,
            "type": board_type,
        })
    return boards


def _load_pipeline_stages_from_source() -> list[str]:
    if not PIPELINE_PATH.exists():
        raise HTTPException(status_code=500, detail=f"Pipeline source file not found: {PIPELINE_PATH}")

    try:
        source_text = PIPELINE_PATH.read_text()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed reading pipeline source file {PIPELINE_PATH}: {exc}") from exc

    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid Python syntax in {PIPELINE_PATH}: {exc}") from exc

    raw = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STAGE_ORDER":
                    try:
                        raw = ast.literal_eval(node.value)
                    except (SyntaxError, ValueError) as exc:
                        raise HTTPException(
                            status_code=500,
                            detail=f"Invalid STAGE_ORDER constant in {PIPELINE_PATH}: {exc}",
                        ) from exc
                    break
            if raw is not None:
                break
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "STAGE_ORDER":
                try:
                    raw = ast.literal_eval(node.value)
                except (SyntaxError, ValueError) as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Invalid STAGE_ORDER constant in {PIPELINE_PATH}: {exc}",
                    ) from exc
                break

    if raw is None:
        raise HTTPException(status_code=500, detail=f"STAGE_ORDER not found in {PIPELINE_PATH}")
    if not isinstance(raw, (tuple, list)):
        raise HTTPException(status_code=500, detail=f"STAGE_ORDER must be a list or tuple in {PIPELINE_PATH}")

    stage_names: list[str] = []
    for item in raw:
        stage = str(item).strip()
        if stage and stage not in stage_names:
            stage_names.append(stage)
    return stage_names


def _load_profile_json_required() -> dict:
    _require_file_exists(PROFILE_PATH, "Profile")
    try:
        data = json.loads(PROFILE_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {PROFILE_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"Profile JSON must be an object: {PROFILE_PATH}")
    return data


def _default_board_sites() -> list[str]:
    try:
        ids = [str(b.get("id", "")).strip() for b in _load_jobspy_boards_from_source()]
        cleaned = [board_id for board_id in ids if board_id]
        if cleaned:
            return cleaned
    except HTTPException:
        pass
    return []


def _default_profile() -> dict:
    return {
        "min_score": DEFAULT_MIN_SCORE,
        "personal": {
            "full_name": "",
            "preferred_name": "",
            "email": "",
            "phone": "",
            "city": "",
            "province_state": "",
        },
        "experience": {
            "target_role": "",
            "years_of_experience_total": "",
        },
        "compensation": {
            "salary_range_min": "",
            "salary_range_max": "",
        },
        "skills_boundary": {},
    }


def _default_searches() -> dict:
    default_boards = _default_board_sites()
    return {
        "queries": [],
        "locations": [],
        "boards": list(default_boards),
        "sites": list(default_boards),
    }


def _get_profile_min_score_default() -> int:
    if not PROFILE_PATH.exists():
        return DEFAULT_MIN_SCORE
    data = _load_profile_json_required()
    raw = data.get("min_score")
    if raw is None:
        return DEFAULT_MIN_SCORE
    return _coerce_min_score(raw, source="profile")


def _initialize_jobs_db():
    _ensure_config_dir()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                salary TEXT,
                location TEXT,
                site TEXT,
                strategy TEXT,
                discovered_at TEXT,
                description TEXT,
                full_description TEXT,
                application_url TEXT,
                detail_error TEXT,
                fit_score INTEGER,
                score_reasoning TEXT,
                scored_at TEXT,
                tailored_resume_path TEXT,
                tailored_at TEXT,
                cover_letter_path TEXT,
                cover_letter_at TEXT,
                applied_at TEXT,
                apply_status TEXT,
                apply_error TEXT
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_fit_score ON jobs(fit_score)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_applied_at ON jobs(applied_at)")
        conn.commit()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Failed to initialize database {DB_PATH}: {exc}") from exc
    finally:
        conn.close()


# ═══ SERVE FRONTEND ═══

@app.get("/")
async def serve_frontend():
    return FileResponse(Path(__file__).parent / "ui-prototype.html")


# ═══ STATS ═══

@app.get("/api/stats")
async def get_stats():
    if not DB_PATH.exists():
        return {
            "total": 0,
            "enriched": 0,
            "scored": 0,
            "scored_7plus": 0,
            "tailored": 0,
            "cover_letters": 0,
            "applied": 0,
            "last_discovered": None,
            "sources": {},
        }
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM jobs")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL")
    enriched = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL")
    scored = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= ?", (DEFAULT_MIN_SCORE,))
    scored_7plus = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL")
    tailored = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL")
    cover_letters = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL")
    applied = c.fetchone()[0]
    # Recent discovery time
    c.execute("SELECT MAX(discovered_at) FROM jobs")
    last_discovered = c.fetchone()[0]
    # Source breakdown
    c.execute("SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC")
    sources = {r["site"]: r["cnt"] for r in c.fetchall()}
    conn.close()
    return {
        "total": total, "enriched": enriched, "scored": scored,
        "scored_7plus": scored_7plus, "tailored": tailored, "cover_letters": cover_letters, "applied": applied,
        "last_discovered": last_discovered, "sources": sources
    }


# ═══ JOBS ═══

@app.get("/api/jobs")
async def get_jobs(
    search: str = "",
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    status: Optional[str] = None,
    location: Optional[str] = None,
    site: Optional[str] = None,
    sort: str = "discovered_at",
    order: str = "desc",
    limit: int = 200,
    offset: int = 0
):
    if not DB_PATH.exists():
        return {"jobs": [], "total": 0}
    conn = get_db()
    c = conn.cursor()

    where_clauses = []
    params = []

    if search:
        where_clauses.append("(title LIKE ? OR description LIKE ? OR location LIKE ? OR site LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s, s])

    if min_score is not None:
        where_clauses.append("fit_score >= ?")
        params.append(min_score)
    if max_score is not None:
        where_clauses.append("fit_score <= ?")
        params.append(max_score)

    if status == "discovered":
        where_clauses.append("full_description IS NULL AND fit_score IS NULL")
    elif status == "enriched":
        where_clauses.append("full_description IS NOT NULL AND fit_score IS NULL")
    elif status == "scored":
        where_clauses.append("fit_score IS NOT NULL AND tailored_resume_path IS NULL")
    elif status == "tailored":
        where_clauses.append("tailored_resume_path IS NOT NULL AND applied_at IS NULL")
    elif status == "applied":
        where_clauses.append("applied_at IS NOT NULL")

    if location:
        where_clauses.append("location LIKE ?")
        params.append(f"%{location}%")

    if site:
        where_clauses.append("site = ?")
        params.append(site)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    allowed_sorts = {"title", "salary", "location", "site", "fit_score", "discovered_at", "scored_at", "applied_at"}
    sort_col = sort if sort in allowed_sorts else "discovered_at"
    sort_order = "ASC" if order.lower() == "asc" else "DESC"
    nulls = "NULLS LAST" if sort_col == "fit_score" else ""

    # Count total matching
    c.execute(f"SELECT COUNT(*) FROM jobs {where}", params)
    total = c.fetchone()[0]

    # Fetch page
    c.execute(
        f"""SELECT url, title, salary, location, site, strategy, discovered_at,
                   fit_score, score_reasoning, scored_at,
                   tailored_resume_path, tailored_at,
                   cover_letter_path, cover_letter_at,
                   applied_at, apply_status, apply_error,
                   full_description, application_url, detail_error
            FROM jobs {where}
            ORDER BY {sort_col} {sort_order} {nulls}
            LIMIT ? OFFSET ?""",
        params + [limit, offset]
    )
    jobs = [row_to_dict(r) for r in c.fetchall()]

    # Derive a status field for each job
    for j in jobs:
        if j.get("applied_at"):
            j["status"] = "applied"
        elif j.get("tailored_resume_path"):
            j["status"] = "tailored"
        elif j.get("fit_score") is not None:
            j["status"] = "scored"
        elif j.get("full_description"):
            j["status"] = "enriched"
        else:
            j["status"] = "discovered"
        # Extract company from title or site for display
        j["company"] = j.get("site", "").replace("_", " ").title() if j.get("site") else ""

    conn.close()
    return {"jobs": jobs, "total": total}


@app.get("/api/jobs/detail")
async def get_job_detail(url: str):
    _require_db_exists()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM jobs WHERE url = ?", (url,))
    job = row_to_dict(c.fetchone())
    conn.close()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Read tailored resume if exists
    if job.get("tailored_resume_path") and os.path.exists(job["tailored_resume_path"]):
        with open(job["tailored_resume_path"]) as f:
            job["tailored_resume_text"] = f.read()
    # Read cover letter if exists
    if job.get("cover_letter_path") and os.path.exists(job["cover_letter_path"]):
        with open(job["cover_letter_path"]) as f:
            job["cover_letter_text"] = f.read()
    return job


# ═══ BOARDS ═══

@app.get("/api/boards")
async def get_boards():
    def normalize_key(value: str) -> str:
        return re.sub(r"[^a-z0-9_]", "", str(value).strip().lower().replace("-", "_").replace(" ", "_"))

    sites_cfg = load_sites_config()
    if not isinstance(sites_cfg, dict):
        raise HTTPException(status_code=500, detail="sites.yaml must parse as an object")

    blocked_sites_raw, blocked_url_patterns = load_blocked_sites()
    blocked_sites_map: dict[str, str] = {}
    for item in blocked_sites_raw:
        text = str(item).strip()
        if text:
            blocked_sites_map[text.lower()] = text
            blocked_sites_map[normalize_key(text)] = text

    boards_by_key: dict[str, dict] = {}
    board_order: list[str] = []

    def upsert_board(name: str, source: str, board_type: str, board_id: Optional[str] = None, url: Optional[str] = None):
        key = name.strip().lower()
        slug = normalize_key(board_id or name)
        if not key or not slug:
            return
        is_blocked = key in blocked_sites_map or normalize_key(key) in blocked_sites_map or slug in blocked_sites_map
        block_reason = "anti-bot protection"

        if slug not in boards_by_key:
            entry = {
                "id": slug,
                "name": name,
                "source": source,
                "type": board_type,
                "blocked": is_blocked,
            }
            if url:
                entry["url"] = url
            if is_blocked:
                entry["block_reason"] = block_reason
            boards_by_key[slug] = entry
            board_order.append(slug)
            return

        existing = boards_by_key[slug]
        if url and "url" not in existing:
            existing["url"] = url
        if is_blocked:
            existing["blocked"] = True
            existing["block_reason"] = block_reason

    for board in _load_jobspy_boards_from_source():
        upsert_board(
            name=board["name"],
            source="jobspy",
            board_type=str(board.get("type") or "search"),
            board_id=str(board.get("id") or board.get("name") or ""),
        )

    sites = sites_cfg.get("sites") or []
    if not isinstance(sites, list):
        raise HTTPException(status_code=500, detail="sites.yaml field 'sites' must be a list")

    for site in sites:
        if not isinstance(site, dict):
            continue
        name = str(site.get("name", "")).strip()
        if not name:
            continue
        board_type = str(site.get("type") or "static")
        url = site.get("url")
        upsert_board(
            name=name,
            source="sites_yaml",
            board_type=board_type,
            board_id=name,
            url=str(url) if url else None,
        )

    for key, original_name in blocked_sites_map.items():
        if key in boards_by_key:
            continue
        upsert_board(
            name=original_name,
            source="sites_yaml",
            board_type="static",
            board_id=original_name,
        )

    manual_ats = sites_cfg.get("manual_ats") or []
    if not isinstance(manual_ats, list):
        raise HTTPException(status_code=500, detail="sites.yaml field 'manual_ats' must be a list")

    blocked_sso = load_blocked_sso()
    if not isinstance(blocked_sso, list):
        raise HTTPException(status_code=500, detail="sites.yaml field 'blocked_sso' must be a list")

    return {
        "boards": [boards_by_key[k] for k in board_order],
        "manual_ats": manual_ats,
        "blocked_sso": blocked_sso,
        "blocked_url_patterns": blocked_url_patterns,
    }


# ═══ CONFIG ═══

@app.get("/api/config/defaults")
async def get_config_defaults():
    pipeline_stages_internal = _load_pipeline_stages_from_source()
    pipeline_stages: list[str] = []
    for stage in pipeline_stages_internal:
        external = "cover_letter" if stage == "cover" else stage
        if external not in pipeline_stages:
            pipeline_stages.append(external)
    if "apply" not in pipeline_stages:
        pipeline_stages.append("apply")

    if not isinstance(APPLYPILOT_DEFAULTS, dict):
        raise HTTPException(status_code=500, detail="DEFAULTS must be a dict in applypilot config.py")
    if not isinstance(APPLYPILOT_SCORE_FILTERS, dict):
        raise HTTPException(status_code=500, detail="SCORE_FILTERS must be a dict in applypilot config.py")
    if not isinstance(APPLYPILOT_TIER_LABELS, dict):
        raise HTTPException(status_code=500, detail="TIER_LABELS must be a dict in applypilot config.py")

    return {
        "defaults": dict(APPLYPILOT_DEFAULTS),
        "pipeline_stages": pipeline_stages,
        "score_filters": dict(APPLYPILOT_SCORE_FILTERS),
        "tier_labels": {str(k): v for k, v in APPLYPILOT_TIER_LABELS.items()},
    }


@app.get("/api/config/profile")
async def get_profile():
    if not PROFILE_PATH.exists():
        return _default_profile()
    data = _load_profile_json_required()
    data["min_score"] = _coerce_min_score(data.get("min_score", DEFAULT_MIN_SCORE), source="profile")
    for key, value in _default_profile().items():
        if key not in data:
            data[key] = value
    return data


@app.put("/api/config/profile")
async def save_profile(data: dict):
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Profile payload must be a JSON object")
    min_score = _coerce_min_score(data.get("min_score", DEFAULT_MIN_SCORE), source="request")
    payload = dict(data)
    payload["min_score"] = min_score
    _ensure_config_dir()
    try:
        PROFILE_PATH.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed writing profile to {PROFILE_PATH}: {exc}") from exc
    return {"ok": True, "min_score": min_score}


@app.get("/api/config/searches")
async def get_searches():
    if not SEARCHES_PATH.exists():
        return _default_searches()
    try:
        loaded = yaml.safe_load(SEARCHES_PATH.read_text())
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid YAML in {SEARCHES_PATH}: {exc}") from exc
    if loaded is None:
        return _default_searches()
    if not isinstance(loaded, dict):
        raise HTTPException(status_code=500, detail=f"Searches YAML must be an object: {SEARCHES_PATH}")
    defaults = _default_searches()
    for key, value in defaults.items():
        if key not in loaded:
            loaded[key] = value
    boards = loaded.get("boards")
    sites = loaded.get("sites")
    if isinstance(boards, list) and not isinstance(sites, list):
        loaded["sites"] = list(boards)
    if isinstance(sites, list) and not isinstance(boards, list):
        loaded["boards"] = list(sites)
    if not isinstance(loaded.get("boards"), list):
        loaded["boards"] = list(defaults["boards"])
    if not isinstance(loaded.get("sites"), list):
        loaded["sites"] = list(loaded["boards"])
    return loaded


@app.put("/api/config/searches")
async def save_searches(data: dict):
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Searches payload must be a JSON object")
    payload = dict(data)
    boards = payload.get("boards")
    sites = payload.get("sites")
    if isinstance(boards, list) and not isinstance(sites, list):
        payload["sites"] = list(boards)
    if isinstance(sites, list) and not isinstance(boards, list):
        payload["boards"] = list(sites)
    if not isinstance(payload.get("boards"), list):
        payload["boards"] = []
    if not isinstance(payload.get("sites"), list):
        payload["sites"] = list(payload["boards"])
    _ensure_config_dir()
    try:
        SEARCHES_PATH.write_text(yaml.dump(payload, default_flow_style=False))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed writing searches to {SEARCHES_PATH}: {exc}") from exc
    return {"ok": True}


@app.get("/api/config/resume")
async def get_resume():
    if not RESUME_PATH.exists():
        return {"text": ""}
    try:
        return {"text": RESUME_PATH.read_text()}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed reading resume file {RESUME_PATH}: {exc}") from exc


@app.put("/api/config/resume")
async def save_resume(data: dict):
    if "text" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: text")
    text = data["text"]
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Field 'text' must be a string")
    _ensure_config_dir()
    try:
        RESUME_PATH.write_text(text)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed writing resume to {RESUME_PATH}: {exc}") from exc
    return {"ok": True, "chars": len(text)}


@app.get("/api/config/capsolver")
async def get_capsolver_config():
    key = _read_env_value(ENV_PATH, "CAPSOLVER_API_KEY")
    return {
        "configured": bool(key),
        "key_hint": _mask_key_hint(key),
    }


@app.put("/api/config/capsolver")
async def save_capsolver_config(data: dict):
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise HTTPException(status_code=400, detail="Field 'key' must be a non-empty string")
    clean_key = key.strip()
    _upsert_env_value(ENV_PATH, "CAPSOLVER_API_KEY", clean_key)
    return {
        "ok": True,
        "configured": True,
        "key_hint": _mask_key_hint(clean_key),
    }


@app.get("/api/config/env")
async def get_env_config():
    key = _read_env_value(ENV_PATH, "CAPSOLVER_API_KEY") or ""
    return {
        "CAPSOLVER_API_KEY": key,
        "capsolver_configured": bool(key),
        "capsolver_masked": _mask_key_hint(key),
        "env_path": str(ENV_PATH),
    }


@app.put("/api/config/env")
async def save_env_config(data: dict):
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Env payload must be a JSON object")
    raw = data.get("CAPSOLVER_API_KEY", data.get("capsolver_api_key"))
    if raw is None:
        raise HTTPException(status_code=400, detail="Missing CAPSOLVER_API_KEY in payload")
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="CAPSOLVER_API_KEY must be a string")
    key = raw.strip()
    if key:
        _upsert_env_value(ENV_PATH, "CAPSOLVER_API_KEY", key)
    else:
        _remove_env_key(ENV_PATH, "CAPSOLVER_API_KEY")
    return {
        "ok": True,
        "CAPSOLVER_API_KEY": key,
        "capsolver_configured": bool(key),
        "capsolver_masked": _mask_key_hint(key),
        "env_path": str(ENV_PATH),
    }


@app.get("/api/jobs/export")
async def export_jobs_csv():
    _require_db_exists()
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT url, title, salary, location, site, strategy, discovered_at,
                        fit_score, score_reasoning, scored_at, applied_at, apply_status
                 FROM jobs ORDER BY fit_score DESC NULLS LAST""")
    rows = c.fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["URL", "Title", "Salary", "Location", "Source", "Strategy",
                     "Discovered", "Score", "Score Reasoning", "Scored At", "Applied At", "Apply Status"])
    for r in rows:
        writer.writerow([r["url"], r["title"], r["salary"], r["location"], r["site"],
                        r["strategy"], r["discovered_at"], r["fit_score"], r["score_reasoning"],
                        r["scored_at"], r["applied_at"], r["apply_status"]])
    output.seek(0)
    return StreamingResponse(
        output, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applypilot-jobs.csv"}
    )


# ═══ PIPELINE ═══

@app.post("/api/pipeline/run")
async def run_pipeline(request: Request):
    global _pipeline_proc, _pipeline_meta
    if _pipeline_proc and _pipeline_proc.poll() is None:
        return {"error": "Pipeline already running", "pid": _pipeline_proc.pid}

    stage_text = None
    min_score_raw = None
    workers_raw = None
    dry_run_raw = None
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/x-www-form-urlencoded" in content_type:
        body_bytes = await request.body()
        try:
            form_data = parse_qs(body_bytes.decode("utf-8"), keep_blank_values=False)
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid form payload encoding: {exc}") from exc
        stage_values = form_data.get("stages")
        min_score_values = form_data.get("min_score")
        workers_values = form_data.get("workers")
        dry_run_values = form_data.get("dry_run")
        if stage_values:
            stage_text = stage_values[0]
        if min_score_values:
            min_score_raw = min_score_values[0]
        if workers_values:
            workers_raw = workers_values[0]
        if dry_run_values:
            dry_run_raw = dry_run_values[0]
    elif "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Unable to parse multipart form payload: {exc}") from exc
        stage_text = form.get("stages")
        min_score_raw = form.get("min_score")
        workers_raw = form.get("workers")
        dry_run_raw = form.get("dry_run")
    elif "application/json" in content_type:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc
        if isinstance(body, dict):
            stage_text = body.get("stages")
            min_score_raw = body.get("min_score")
            workers_raw = body.get("workers")
            dry_run_raw = body.get("dry_run")

    if stage_text in (None, ""):
        stage_text = request.query_params.get("stages")
    if min_score_raw in (None, ""):
        min_score_raw = request.query_params.get("min_score")
    if workers_raw in (None, ""):
        workers_raw = request.query_params.get("workers")
    if dry_run_raw in (None, ""):
        dry_run_raw = request.query_params.get("dry_run")

    if isinstance(stage_text, list):
        stage_text = ",".join(str(item) for item in stage_text)
    elif stage_text is not None and not isinstance(stage_text, str):
        stage_text = str(stage_text)

    resolved_min_score = (
        _coerce_min_score(min_score_raw, source="request")
        if min_score_raw not in (None, "")
        else _get_profile_min_score_default()
    )
    resolved_workers = (
        _coerce_workers(workers_raw, source="request")
        if workers_raw not in (None, "")
        else 1
    )
    resolved_dry_run = (
        _coerce_bool(dry_run_raw, source="request")
        if dry_run_raw not in (None, "")
        else False
    )
    pipeline_stages = _load_pipeline_stages_from_source()
    default_stage = pipeline_stages[0] if pipeline_stages else "discover"
    requested = [s.strip() for s in str(stage_text or default_stage).split(",") if s.strip()]
    aliases = {
        "cover_letter": "cover",
        "coverletter": "cover",
    }
    normalized = [aliases.get(s, s) for s in requested]
    valid_run_stages = set(pipeline_stages)
    includes_apply = "apply" in normalized
    run_stages = [s for s in normalized if s != "apply"]
    unknown = [s for s in run_stages if s not in valid_run_stages]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported stage(s): {', '.join(unknown)}")

    env = _load_env()
    if includes_apply and not run_stages:
        cmd = [str(APPLYPILOT_BIN), "apply", "--min-score", str(resolved_min_score), "--workers", str(resolved_workers)]
        if resolved_dry_run:
            cmd.append("--dry-run")
        command_repr = " ".join(shlex.quote(part) for part in cmd)
        _pipeline_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    else:
        if not run_stages:
            run_stages = [default_stage]
        run_stage_text = ",".join(run_stages)
        run_cmd = [str(APPLYPILOT_BIN), "run", "--stages", run_stage_text, "--min-score", str(resolved_min_score), "--workers", str(resolved_workers)]
        if resolved_dry_run:
            run_cmd.append("--dry-run")
        if includes_apply:
            run_cmd_str = " ".join(shlex.quote(part) for part in run_cmd)
            apply_cmd = [str(APPLYPILOT_BIN), "apply", "--min-score", str(resolved_min_score), "--workers", str(resolved_workers)]
            if resolved_dry_run:
                apply_cmd.append("--dry-run")
            apply_cmd_str = " ".join(shlex.quote(part) for part in apply_cmd)
            chained = f"{run_cmd_str} && {apply_cmd_str}"
            command_repr = chained
            _pipeline_proc = subprocess.Popen(
                ["/bin/zsh", "-lc", chained],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        else:
            command_repr = " ".join(shlex.quote(part) for part in run_cmd)
            _pipeline_proc = subprocess.Popen(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )

    _pipeline_meta = {
        "stages": ",".join(requested),
        "resolved_stages": ",".join(normalized),
        "min_score": resolved_min_score,
        "workers": resolved_workers,
        "dry_run": resolved_dry_run,
        "command": command_repr,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "returncode": None,
        "output": "",
        "output_captured": False,
    }
    return {
        "ok": True,
        "pid": _pipeline_proc.pid,
        "stages": _pipeline_meta["stages"],
        "resolved_stages": _pipeline_meta["resolved_stages"],
        "min_score": resolved_min_score,
        "workers": resolved_workers,
        "dry_run": resolved_dry_run,
        "command": command_repr,
    }


@app.get("/api/pipeline/status")
async def pipeline_status():
    global _pipeline_proc, _pipeline_meta
    if _pipeline_proc is None:
        return {
            "running": False,
            "pid": None,
            "stages": _pipeline_meta.get("stages"),
            "resolved_stages": _pipeline_meta.get("resolved_stages"),
            "min_score": _pipeline_meta.get("min_score"),
            "workers": _pipeline_meta.get("workers"),
            "dry_run": _pipeline_meta.get("dry_run"),
            "started_at": _pipeline_meta.get("started_at"),
            "finished_at": _pipeline_meta.get("finished_at"),
            "returncode": _pipeline_meta.get("returncode"),
            "output": _pipeline_meta.get("output", ""),
        }
    running = _pipeline_proc.poll() is None
    if not running and not _pipeline_meta.get("output_captured"):
        _pipeline_meta["output"] = _pipeline_proc.stdout.read() if _pipeline_proc.stdout else ""
        _pipeline_meta["output_captured"] = True
        _pipeline_meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        _pipeline_meta["returncode"] = _pipeline_proc.returncode
    return {
        "running": running,
        "pid": _pipeline_proc.pid if _pipeline_proc else None,
        "stages": _pipeline_meta.get("stages"),
        "resolved_stages": _pipeline_meta.get("resolved_stages"),
        "min_score": _pipeline_meta.get("min_score"),
        "workers": _pipeline_meta.get("workers"),
        "dry_run": _pipeline_meta.get("dry_run"),
        "started_at": _pipeline_meta.get("started_at"),
        "finished_at": _pipeline_meta.get("finished_at"),
        "returncode": _pipeline_meta.get("returncode"),
        "output": _pipeline_meta.get("output", ""),
    }


@app.post("/api/pipeline/stop")
async def stop_pipeline():
    global _pipeline_proc, _pipeline_meta
    if _pipeline_proc and _pipeline_proc.poll() is None:
        _pipeline_proc.terminate()
        _pipeline_meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        _pipeline_meta["returncode"] = _pipeline_proc.poll()
        return {"ok": True, "message": "Pipeline stopped"}
    return {"ok": True, "message": "No pipeline running"}


# ═══ SYSTEM ═══

@app.get("/api/system/check")
async def system_check():
    checks = []
    # Python
    py_version = sys.version.split()[0]
    checks.append({"name": "Python", "detail": py_version, "ok": True})
    # Venv
    checks.append({"name": "ApplyPilot venv", "detail": str(VENV_PYTHON.parent.parent), "ok": VENV_PYTHON.exists()})
    # Database
    checks.append({"name": "Database", "detail": str(DB_PATH), "ok": DB_PATH.exists()})
    # Config files
    checks.append({"name": "Profile", "detail": str(PROFILE_PATH), "ok": PROFILE_PATH.exists()})
    checks.append({"name": "Searches", "detail": str(SEARCHES_PATH), "ok": SEARCHES_PATH.exists()})
    checks.append({"name": "Resume", "detail": str(RESUME_PATH), "ok": RESUME_PATH.exists()})
    # Gemini CLI
    gemini_path = shutil.which("gemini")
    checks.append({
        "name": "Gemini CLI",
        "detail": gemini_path if gemini_path else "Not found on PATH",
        "ok": bool(gemini_path),
    })
    return {"checks": checks}


@app.post("/api/system/open-config")
async def open_config_folder():
    _ensure_config_dir()
    target = str(CONFIG_DIR)
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", target])
        elif os.name == "nt":
            subprocess.Popen(["explorer", target])
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                raise HTTPException(status_code=500, detail="Unable to open folder, xdg-open not found")
            subprocess.Popen([opener, target])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open config folder {target}: {exc}") from exc
    return {"ok": True, "path": target}


@app.post("/api/system/reset-database")
async def reset_database():
    global _pipeline_proc
    if _pipeline_proc and _pipeline_proc.poll() is None:
        raise HTTPException(status_code=409, detail="Cannot reset database while pipeline is running")
    _ensure_config_dir()

    backup_path = None
    if DB_PATH.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = CONFIG_DIR / f"{DB_PATH.name}.bak.{stamp}"
        try:
            DB_PATH.replace(backup_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to backup database {DB_PATH}: {exc}") from exc

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(DB_PATH) + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Failed to remove SQLite sidecar file {sidecar}: {exc}") from exc

    _initialize_jobs_db()
    return {
        "ok": True,
        "path": str(DB_PATH),
        "backup_path": str(backup_path) if backup_path else "",
        "message": "Database reset complete",
    }


@app.get("/api/system/checks")
async def system_checks():
    return await system_check()


if __name__ == "__main__":
    import uvicorn
    print(f"Starting ApplyPilot UI at http://localhost:8888")
    print(f"Database: {DB_PATH} ({'exists' if DB_PATH.exists() else 'not found'})")
    print(f"Config: {CONFIG_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8888)
