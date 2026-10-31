"""choose_model: off means always strong, on means the saved classifier decides or it fails."""

from collections.abc import Iterator
from pathlib import Path

import joblib
import pytest

from brigada.config import Settings
from brigada.router import _load, choose_model
from brigada.router.train import fit, save_model

from .test_train import synthetic


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    _load.cache_clear()
    yield
    _load.cache_clear()


def _settings(path: Path, enabled: bool) -> Settings:
    return Settings(
        router_enabled=enabled,
        router_model_path=str(path),
        model_fast="fast-model",
        model_strong="strong-model",
    )


@pytest.fixture
def trained(tmp_path: Path) -> Path:
    path = tmp_path / "router.joblib"
    rows = synthetic()
    save_model(fit(rows), path, n=len(rows))
    return path


def test_off_by_default() -> None:
    assert Settings().router_enabled is False


def test_off_is_always_strong_and_reads_no_file(tmp_path: Path) -> None:
    s = _settings(tmp_path / "missing.joblib", enabled=False)
    assert choose_model("Quanto vendi ontem?", s) == "strong-model"
    assert _load.cache_info().currsize == 0


def test_off_ignores_a_trained_model(trained: Path) -> None:
    assert choose_model("Quanto vendi ontem?", _settings(trained, enabled=False)) == "strong-model"


def test_on_routes_by_the_classifier(trained: Path) -> None:
    s = _settings(trained, enabled=True)
    assert choose_model("Quanto vendi na sexta?", s) == "fast-model"
    assert choose_model("Por que o movimento caiu e o que devo mudar?", s) == "strong-model"


def test_on_without_a_model_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="router.train"):
        choose_model("Quanto vendi ontem?", _settings(tmp_path / "missing.joblib", enabled=True))


def test_on_with_a_file_that_is_not_a_router_fails(tmp_path: Path) -> None:
    path = tmp_path / "other.joblib"
    joblib.dump({"something": 1}, path)
    with pytest.raises(ValueError, match="not a router model"):
        choose_model("Quanto vendi ontem?", _settings(path, enabled=True))


def test_threshold_comes_from_the_saved_bundle(tmp_path: Path) -> None:
    path = tmp_path / "router.joblib"
    rows = synthetic()
    save_model(fit(rows), path, n=len(rows), threshold=1.01)  # nothing reaches it
    s = _settings(path, enabled=True)
    assert choose_model("Quanto vendi na sexta?", s) == "strong-model"
