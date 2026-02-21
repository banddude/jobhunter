# ApplyPilot

AI powered job application pipeline with a native macOS desktop app. ApplyPilot discovers job listings, scores them against your profile, tailors resumes, generates cover letters, and can even auto apply, all orchestrated through a clean web UI.

## Features

- **Job Discovery**: Scrapes multiple job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, and more) using configurable search queries and locations
- **AI Scoring**: Scores each listing against your resume and preferences using Gemini, so you focus on the best fits
- **Resume Tailoring**: Generates a tailored resume for each high scoring job
- **Cover Letters**: Produces targeted cover letters with PDF output
- **Auto Apply**: Optionally submits applications via browser automation (requires Claude Code CLI)
- **Pipeline Control**: Run stages individually or chain them together from the UI
- **Real Time Logs**: Watch pipeline output live as jobs flow through each stage

## Prerequisites

- Python 3.11+
- Node.js 18+ (for Electron wrapper and CLI tools)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) (required for AI scoring, tailoring, and cover letters)
- [Claude Code CLI](https://github.com/anthropics/claude-code) (optional, only needed for auto apply)

## Quick Start

### Option A: Run from source

```bash
git clone https://github.com/banddude/jobhunter.git
cd jobhunter

# Set up Python environment
python3 -m venv applypilot/.venv
source applypilot/.venv/bin/activate
pip install -r requirements.txt
pip install -e applypilot/

# Start the server
python server.py
```

Then open http://localhost:8888 in your browser.

### Option B: Run as a desktop app (Electron)

```bash
git clone https://github.com/banddude/jobhunter.git
cd jobhunter

# Set up Python environment first (same as above)
python3 -m venv applypilot/.venv
source applypilot/.venv/bin/activate
pip install -r requirements.txt
pip install -e applypilot/

# Launch the desktop app
cd electron
npm install
npm start
```

## Configuration

All user configuration lives in `~/.applypilot/`:

| File | Purpose |
|------|---------|
| `profile.json` | Your name, contact info, target role, salary range, and minimum score threshold |
| `searches.yaml` | Search queries, locations, and which job boards to scrape |
| `resume.txt` | Your base resume (plain text), used for AI scoring and tailoring |
| `.env` | Optional API keys (e.g. CAPSOLVER_API_KEY for captcha solving) |

The web UI includes an onboarding wizard that walks you through setting up all of these on first launch.

## Architecture

```
jobhunter/
  server.py            FastAPI backend (all API endpoints)
  ui-prototype.html    Single file web frontend
  applypilot/          Core library (discovery, scoring, tailoring, apply logic)
  electron/            Electron shell for native macOS app
  tests/               API tests
```

- **server.py**: FastAPI app serving the UI and proxying to applypilot's CLI and database
- **ui-prototype.html**: Self contained frontend with no build step
- **applypilot/**: The core pipeline library, installable via pip. Handles job scraping, LLM calls, resume generation, and browser automation
- **electron/**: Thin Electron wrapper that spawns the Python server and loads the UI in a native window

## Acknowledgments

The core pipeline library powering ApplyPilot was created by [Pickle-Pixel](https://github.com/Pickle-Pixel/ApplyPilot). This project wraps it in a web UI and native macOS desktop app for a more accessible experience.

## License

[AGPL-3.0](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.
