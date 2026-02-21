"""
Unified Gemini CLI client for ApplyPilot.

Both LLM tiers use Gemini CLI:
  "quality", resume tailoring and cover letters, Gemini CLI default model
  "bulk", scoring, enrichment, extraction, Gemini CLI default model

Call sites pass tier to get_client():
  get_client("quality"), tailor.py, cover_letter.py
  get_client("bulk"), scorer.py, detail.py, smartextract.py
  get_client(), defaults to bulk
"""

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

_GEMINI_CLI = shutil.which("gemini")


def _messages_to_prompt(messages: list[dict]) -> tuple[str, str]:
    """Convert chat messages to system and user prompts for CLI calls."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        elif m["role"] == "user":
            user_parts.append(m["content"])
        elif m["role"] == "assistant":
            user_parts.append(f"[Previous assistant response]:\n{m['content']}")
    return "\n\n".join(system_parts), "\n\n".join(user_parts)


class GeminiCLIClient:
    """Calls Gemini via bare CLI subprocess."""

    def __init__(self, model: str | None = None):
        self.model = model

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        system, prompt = _messages_to_prompt(messages)
        if system:
            prompt = f"{system}\n\n{prompt}"

        env = {
            **os.environ,
            "NO_COLOR": "1",
        }

        try:
            cmd = [
                _GEMINI_CLI,
                "-p",
                prompt,
                "-o",
                "text",
                "--sandbox",
                "--allowed-mcp-server-names",
                "none",
                "--allowed-tools",
                "none",
                "-e",
                "none",
            ]
            if self.model:
                cmd.extend(["--model", self.model])
            log.info("LLM request: provider=GeminiCLI model=%s", self.model or "default")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (output or "").strip()
                raise RuntimeError(
                    f"Gemini CLI failed (rc={result.returncode}). "
                    f"stderr={stderr[:300]} stdout={stdout[:300]}"
                )
            if not output:
                raise RuntimeError("Gemini CLI returned empty output")
            return output
        except subprocess.TimeoutExpired:
            raise RuntimeError("Gemini CLI timed out after 180s")

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        pass


_quality_instance = None
_bulk_instance = None


def _create_quality_client():
    """Build a client for quality tier, Gemini CLI default model."""
    if _GEMINI_CLI:
        log.info("Quality tier: Gemini CLI default model")
        return GeminiCLIClient()
    raise RuntimeError("Quality tier requested but Gemini CLI was not found in PATH")


def _create_bulk_client():
    """Build a client for bulk tier, Gemini CLI gemini-2.5-flash."""
    if _GEMINI_CLI:
        log.info("Bulk tier: Gemini CLI gemini-2.5-flash")
        return GeminiCLIClient(model="gemini-2.5-flash")
    raise RuntimeError("Bulk tier requested but Gemini CLI was not found in PATH")


def get_client(tier: str = "bulk"):
    """Return a client for the given tier, quality or bulk."""
    global _quality_instance, _bulk_instance
    if tier == "quality":
        if _quality_instance is None:
            _quality_instance = _create_quality_client()
        return _quality_instance
    if _bulk_instance is None:
        _bulk_instance = _create_bulk_client()
    return _bulk_instance
