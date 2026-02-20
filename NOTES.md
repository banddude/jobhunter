# ApplyPilot UI Project Notes

## Project Goal
Build a normie-ready web UI around the ApplyPilot CLI pipeline so anyone can use it without touching a terminal.

## Current State
- ApplyPilot v0.2.0 installed in venv at `~/jobhunter/applypilot/.venv`
- CLI works: `applypilot init`, `applypilot run`, `applypilot apply`, `applypilot status`, `applypilot dashboard`
- Resume created and copied to `~/.applypilot/resume.txt`
- Profile/search config NOT yet completed (wizard was interrupted at Step 2)
- No API key configured yet

## Setup Wizard Flow (what the UI needs to replace)
The CLI wizard (`applypilot init`) walks through these steps:

### Step 1: Resume Upload
- Input: file path to .txt or .pdf resume
- Copies file to `~/.applypilot/resume.txt`
- **UI needs:** File upload component (drag and drop), preview of resume text

### Step 2: Profile (personal info + job preferences)
- Full name
- Preferred name (for cover letter sign-off)
- Email
- Phone
- Location (city, state)
- Work authorization status (US citizen, green card, visa, etc.)
- Target role (what kind of job you want)
- Years of experience
- Education level
- Salary expectations (min, max, currency, hourly/annual)
- Currency conversion note (for international jobs)
- Skills boundary (categories of real skills, used to prevent AI fabrication)
- Resume facts (preserved companies, projects, school, real metrics)
- **UI needs:** Multi-step form wizard with sections, dropdowns, tag inputs for skills

### Step 3: Search Configuration
- Search queries with tier priority (1=exact, 2=strong, 3=wide net)
- Locations (city + remote flag)
- Location accept/reject patterns
- Country for job board searches
- Which job boards to use (indeed, linkedin, glassdoor, zip_recruiter, google)
- Results per site, hours_old filter
- Negative keywords (exclude job titles containing these)
- **UI needs:** Query builder with drag-and-drop priority tiers, location picker, board toggles, keyword tag manager

### Step 4: Gemini CLI / LLM Config
- Gemini CLI detection and status
- Optional: CapSolver API key for CAPTCHAs
- Optional: Proxy config
- **UI needs:** Gemini CLI status check and install guidance

## Config Files Generated (all in ~/.applypilot/)
- `profile.json` - personal data, skills, preferences
- `searches.yaml` - job search queries, locations, boards
- `.env` - optional runtime config
- `resume.txt` - base resume text
- `jobs.db` - SQLite database (created on first run)

## Pipeline Stages (what the dashboard needs to show)

### 1. Discover
- Sources: JobSpy (5 boards), Workday (48 employers), 30+ career sites
- Output: jobs table with url, title, salary, description, location, site
- **UI needs:** "Start Discovery" button, progress indicator, source selection toggles

### 2. Enrich
- Visits each job URL, extracts full description + apply link
- 3-tier extraction: JSON-LD, CSS selectors, LLM fallback
- **UI needs:** Progress bar per job, error/skip indicators

### 3. Score
- LLM rates each job 1-10 against resume
- Output: fit_score, score_reasoning
- **UI needs:** Sortable job table with score column, reasoning tooltip/expand

### 4. Tailor Resume
- Only for jobs scored 7+
- LLM rewrites resume per job, preserving real facts
- Output: tailored_resume_path (txt file)
- **UI needs:** Side-by-side diff view (original vs tailored), edit capability

### 5. Cover Letter
- LLM generates per-job cover letter
- Output: cover_letter_path (txt file)
- **UI needs:** Preview/edit cover letter, regenerate button

### 6. Auto-Apply
- Claude Code + Playwright navigates forms, fills fields, submits
- Supports: parallel workers, dry-run, continuous mode
- **UI needs:** Live status feed per application, start/stop controls, dry-run toggle

## Database Schema (SQLite, single `jobs` table)
- Discovery: url (PK), title, salary, description, location, site, strategy, discovered_at
- Enrichment: full_description, application_url, detail_scraped_at, detail_error
- Scoring: fit_score (1-10), score_reasoning, scored_at
- Tailoring: tailored_resume_path, tailored_at, tailor_attempts
- Cover letter: cover_letter_path, cover_letter_at, cover_attempts
- Application: applied_at, apply_status, apply_error, apply_attempts, agent_id, last_attempted_at, apply_duration_ms, apply_task_id, verification_confidence

## UI Pages Needed
1. **Onboarding Wizard** - Replace CLI init (resume upload, profile, search config, API key)
2. **Dashboard** - Pipeline overview, stats, recent activity
3. **Jobs Table** - Browse/filter/sort all discovered jobs with scores
4. **Job Detail** - Full description, tailored resume, cover letter, apply status
5. **Pipeline Controls** - Run stages, monitor progress, start/stop apply
6. **Settings** - Edit profile, search config, API keys, employer list, blocked sites
7. **Resume Manager** - Upload/edit base resume, view tailored versions

## User Preferences (Mike)
- Wants remote jobs
- Computer-based work, minimal voice/video calls
- Has electrician + admin background
- Location: Los Angeles, CA
- Still figuring out exact target roles

## Tech Decisions (TBD)
- Frontend framework (React, Next.js, Svelte?)
- Backend (FastAPI wrapper around ApplyPilot? Or direct SQLite reads?)
- Hosting (local only? or deployable?)
- Auth (single user? multi-user?)
