"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)
_MAX_REQUESTS_PER_MINUTE = 10


class _SlidingWindowRateLimiter:
    """Thread-safe sliding-window limiter."""

    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            wait_seconds = 0.0
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window_seconds
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return
                wait_seconds = (self._timestamps[0] + self.window_seconds) - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)


def _score_job_task(resume_text: str, job: dict, limiter: _SlidingWindowRateLimiter) -> tuple[dict, dict]:
    limiter.acquire()
    return job, score_job(resume_text, job)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = None
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = None
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        parsed = _parse_score_response(response)
        if parsed["score"] is None:
            return {
                "score": None,
                "keywords": "",
                "reasoning": f"Invalid scoring response format: {response[:500]}",
                "error": True,
                "fatal": False,
            }
        return {
            "score": parsed["score"],
            "keywords": parsed["keywords"],
            "reasoning": parsed["reasoning"],
            "error": False,
            "fatal": False,
        }
    except Exception as e:
        message = str(e)
        lower = message.lower()
        fatal = (
            "terminalquotaerror" in lower
            or "exhausted your daily quota" in lower
            or "authentication" in lower
            or "unauthorized" in lower
        )
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), message)
        return {"score": None, "keywords": "", "reasoning": f"LLM error: {message}", "error": True, "fatal": fatal}


def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 10) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        workers: Number of concurrent scoring workers.

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    if workers < 1:
        raise RuntimeError(f"workers must be >= 1, got {workers}")
    if not RESUME_PATH.exists():
        raise RuntimeError(f"Resume not found: {RESUME_PATH}")
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    try:
        if rescore:
            query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
            params = []
            if limit > 0:
                query += " LIMIT ?"
                params.append(limit)
            jobs = conn.execute(query, params).fetchall()
        else:
            jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

        if not jobs:
            log.info("No unscored jobs with descriptions found.")
            return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

        # Convert sqlite3.Row to dicts if needed
        if jobs and not isinstance(jobs[0], dict):
            columns = jobs[0].keys()
            jobs = [dict(zip(columns, row)) for row in jobs]

        log.info(
            "Scoring %d jobs with %d worker(s), rate-limited to %d requests/min...",
            len(jobs), workers, _MAX_REQUESTS_PER_MINUTE,
        )
        t0 = time.time()
        completed = 0
        scored = 0
        errors = 0
        limiter = _SlidingWindowRateLimiter(_MAX_REQUESTS_PER_MINUTE, 60.0)
        fatal_error: RuntimeError | None = None

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="score-worker") as executor:
            jobs_iter = iter(jobs)
            inflight: dict = {}

            def submit_next() -> bool:
                try:
                    next_job = next(jobs_iter)
                except StopIteration:
                    return False
                future = executor.submit(_score_job_task, resume_text, next_job, limiter)
                inflight[future] = True
                return True

            initial_launches = min(workers, len(jobs))
            for idx in range(initial_launches):
                if not submit_next():
                    break
                if idx < (initial_launches - 1):
                    time.sleep(1.0)

            while inflight:
                done, _ = wait(tuple(inflight.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    inflight.pop(fut, None)
                    try:
                        job, result = fut.result()
                    except Exception as exc:
                        completed += 1
                        errors += 1
                        log.error("[%d/%d] score=ERR  worker failure | %s", completed, len(jobs), str(exc)[:240])
                        if fatal_error is None:
                            submit_next()
                        continue

                    completed += 1
                    score_value = result.get("score")
                    if isinstance(score_value, int) and 1 <= score_value <= 10:
                        now = datetime.now(timezone.utc).isoformat()
                        conn.execute(
                            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
                            (score_value, f"{result['keywords']}\n{result['reasoning']}", now, job["url"]),
                        )
                        conn.commit()
                        scored += 1
                        log.info(
                            "[%d/%d] score=%d  %s",
                            completed, len(jobs), score_value, job.get("title", "?")[:60],
                        )
                    else:
                        errors += 1
                        log.error(
                            "[%d/%d] score=ERR  %s | %s",
                            completed, len(jobs), job.get("title", "?")[:60], result.get("reasoning", "unknown error")[:240],
                        )
                        if result.get("fatal") and fatal_error is None:
                            fatal_error = RuntimeError(
                                "Scoring aborted due to Gemini quota/auth error. "
                                "Fix Gemini CLI access or model availability, then re-run score."
                            )

                    if fatal_error is None:
                        submit_next()

                if fatal_error is not None:
                    for fut in list(inflight.keys()):
                        fut.cancel()
                    break

        if fatal_error is not None:
            raise fatal_error

        elapsed = time.time() - t0
        log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", scored, elapsed, scored / elapsed if elapsed > 0 else 0)

        # Score distribution
        dist = conn.execute("""
            SELECT fit_score, COUNT(*) FROM jobs
            WHERE fit_score IS NOT NULL
            GROUP BY fit_score ORDER BY fit_score DESC
        """).fetchall()
        distribution = [(row[0], row[1]) for row in dist]

        return {
            "scored": scored,
            "errors": errors,
            "elapsed": elapsed,
            "distribution": distribution,
        }
    finally:
        conn.close()
