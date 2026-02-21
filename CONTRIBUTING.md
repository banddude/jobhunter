# Contributing to ApplyPilot

Thanks for your interest in contributing. This document covers how to set up a development environment and submit changes.

## Development Setup

1. Clone the repo and set up the Python environment:

```bash
git clone https://github.com/banddude/jobhunter.git
cd jobhunter
python3 -m venv applypilot/.venv
source applypilot/.venv/bin/activate
pip install -r requirements.txt
pip install -e applypilot/
```

2. Start the dev server:

```bash
python server.py
```

The UI is at http://localhost:8888. Changes to `ui-prototype.html` are picked up on refresh (no build step). Changes to `server.py` require restarting the server.

3. (Optional) Run the Electron wrapper:

```bash
cd electron
npm install
npm start
```

## Running Tests

```bash
source applypilot/.venv/bin/activate
pytest tests/
```

## Project Structure

- `server.py`: All backend API endpoints (FastAPI)
- `ui-prototype.html`: The entire frontend in a single HTML file
- `applypilot/`: Core pipeline library (discovery, scoring, tailoring, apply)
- `electron/`: Electron wrapper for native desktop app
- `tests/`: API and integration tests

## Pull Request Process

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run `pytest tests/` and confirm all tests pass
4. Open a pull request with a clear description of what changed and why
5. Keep PRs focused, one feature or fix per PR

## Code Style

- Python: Follow PEP 8. No strict linter enforced yet, just keep it readable.
- Frontend: The UI is a single HTML file. Keep inline styles and scripts organized by section.
- Commit messages: Short summary line, then a blank line, then details if needed.

## Reporting Issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Python version, OS, and any relevant error output
