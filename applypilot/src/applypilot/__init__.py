"""ApplyPilot — AI-powered end-to-end job application pipeline."""

from importlib import import_module

__version__ = "0.2.0"

_EXPORTS = {
    "config": "applypilot.config",
    "pipeline": "applypilot.pipeline",
    "database": "applypilot.database",
    "tailor": "applypilot.scoring.tailor",
    "cover_letter": "applypilot.scoring.cover_letter",
    "scorer": "applypilot.scoring.scorer",
    "detail": "applypilot.enrichment.detail",
    "smartextract": "applypilot.discovery.smartextract",
}

__all__ = ["__version__", *_EXPORTS.keys()]


def __getattr__(name: str):
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module 'applypilot' has no attribute {name!r}")
    module = import_module(module_path)
    globals()[name] = module
    return module
