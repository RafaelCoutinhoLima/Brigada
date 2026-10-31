"""Model router: pick the fast or the strong model for a question.

Off by default (``ROUTER_ENABLED=false``): every question goes to ``model_strong`` and no model
file is read. When on, the classifier saved by ``python -m brigada.router.train`` decides; a
missing or unreadable model file is an error, never a silent fallback to the strong model.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from brigada.config import Settings, get_settings


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, Any]:
    import joblib

    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(
            f"router is enabled but {path!r} does not exist; run python -m brigada.router.train"
        )
    # joblib is pickle: only load a file this project wrote (path set by the operator).
    bundle = joblib.load(file)
    if not isinstance(bundle, dict) or not {"pipeline", "threshold"} <= bundle.keys():
        raise ValueError(f"{path!r} is not a router model")
    return bundle


def choose_model(question: str, settings: Settings | None = None) -> str:
    """``model_fast`` when the router predicts the fast model is enough, else ``model_strong``."""
    settings = settings or get_settings()
    if not settings.router_enabled:
        return settings.model_strong
    bundle = _load(settings.router_model_path)
    pipeline = bundle["pipeline"]
    proba = pipeline.predict_proba([question])[0][list(pipeline.classes_).index(1)]
    return settings.model_fast if proba >= bundle["threshold"] else settings.model_strong
