"""Task 9 tests: profile allowlisting and the adapter contract.

Profile tests are pure Python (no torch/demucs needed). Adapter behaviour is
tested by monkeypatching the lazy importers with fakes, so no model download,
GPU, or numpy stack is required. The real engine path is covered later by
scheduled fixture runs (plan Section 17.3).
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

import worker.models.demucs as demucs_module
from worker.errors import ErrorCode
from worker.models import (
    DEFAULT_PROFILE_ID,
    ModelProfile,
    SeparationError,
    get_profile,
    validate_profile,
)


def _clear_model_cache() -> None:
    """Tests run in-process; the warm-model cache must not leak between them."""
    demucs_module._LOADED_MODELS.clear()


def test_default_profile_is_allowlisted() -> None:
    profile = get_profile(DEFAULT_PROFILE_ID)
    validate_profile(profile)  # does not raise
    assert profile.model_id == "htdemucs"
    assert profile.model_stems == ("drums", "bass", "other", "vocals")
    # Full-stem mode stays disabled until benchmarks pass (plan Section 4.3).
    assert profile.supported_modes == ("vocals_instrumental",)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(SeparationError) as excinfo:
        get_profile("not_a_profile")
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED


def test_mode_resolves_profile_and_env_override_wins() -> None:
    """full_stems resolves to the 6-stem profile, drum_breakdown to drumsep; an
    explicit STEMIFY_MODEL_PROFILE that supports the mode still wins; the default
    mode keeps the 4-stem profile."""
    from worker.models.profiles import get_profile_for_mode

    assert get_profile_for_mode("full_stems").profile_id == "demucs_6s"
    assert get_profile_for_mode("vocals_instrumental").profile_id == "demucs_default"
    assert get_profile_for_mode("drum_breakdown").profile_id == "drumsep"

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("STEMIFY_MODEL_PROFILE", "demucs_6s")
        assert get_profile_for_mode("vocals_instrumental").profile_id == "demucs_6s"
    finally:
        monkeypatch.undo()


def test_drumsep_profile_invariants() -> None:
    """Roadmap Phase B: the drumsep profile is allowlisted and structurally sane.

    The checkpoint checksum was pinned from the first verified download on the
    reference machine (sha256 prefix aefaa854...); the loader now verifies it
    on every load, so a truncated or tampered artifact is rejected.
    """
    from worker.models.profiles import DRUMSEP_PROFILE, validate_profile

    validate_profile(DRUMSEP_PROFILE)  # does not raise
    assert DRUMSEP_PROFILE.model_stems == ("drums_kick", "drums_snare", "drums_cymbals", "drums_toms")
    assert DRUMSEP_PROFILE.supported_modes == ("drum_breakdown",)
    assert DRUMSEP_PROFILE.checkpoint_checksum == "aefaa854"


def test_tampered_profile_fails_validation() -> None:
    profile = get_profile(DEFAULT_PROFILE_ID)
    tampered = ModelProfile(**{**profile.__dict__, "checkpoint_checksum": "deadbeef"})
    with pytest.raises(SeparationError) as excinfo:
        validate_profile(tampered)
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED


class _FakeTorch:
    """Minimal torch surface: device placement, float32 cast, no_grad."""

    float32 = "float32"

    class cuda:
        @staticmethod
        def is_available() -> bool:
            return False

        @staticmethod
        def empty_cache() -> None:
            return None

    class no_grad:
        def __enter__(self) -> _FakeTorch.no_grad:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    @staticmethod
    def from_numpy(array: Any) -> Any:
        return _FakeTensor(array)


class _FakeTensor:
    """Just enough tensor behaviour for the adapter path."""

    def __init__(self, array: Any) -> None:
        self._array = array

    def astype(self, dtype: Any) -> _FakeTensor:
        return self

    def to(self, device: Any) -> _FakeTensor:
        return self

    def __getitem__(self, item: Any) -> Any:
        return self._array[item]


class _FakeModel:
    """Mirrors the demucs model surface the adapter relies on."""

    def __init__(self, sources: list[str]) -> None:
        self.sources = sources

    def to(self, device: object) -> _FakeModel:
        return self

    def eval(self) -> _FakeModel:
        return self


class _FakePretrained:
    def __init__(self, model: _FakeModel) -> None:
        self._model = model

    def get_model(self, name: str) -> _FakeModel:
        return self._model


class _FakeHasher:
    """sha256 stand-in reporting the allowlisted prefix (a valid checkpoint)."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def update(self, block: bytes) -> None:
        return None

    def hexdigest(self) -> str:
        return self._prefix


def _install_fake_engine(monkeypatch: pytest.MonkeyPatch, profile: Any) -> dict[str, Any]:
    """Patch the lazy importers and demucs.apply; no real torch/demucs needed."""
    import types

    import numpy as np
    captured: dict[str, Any] = {}

    def fake_apply_model(model: _FakeModel, tensor: Any, **kwargs: Any) -> Any:
        # The adapter hands us a (1, channels, samples) tensor wrapper; return
        # an object whose .cpu().numpy() yields the mixture for every source.
        mixture = captured["mixture"]
        sources = len(model.sources)

        class _Estimates:
            def cpu(self) -> _Estimates:
                return self

            def numpy(self) -> Any:
                return np.broadcast_to(
                    mixture, (sources, mixture.shape[0], mixture.shape[1])
                ).copy()

            def __getitem__(self, item: Any) -> Any:
                return self

        return _Estimates()

    def fake_import_torch() -> Any:
        return _FakeTorch

    def fake_import_demucs_pretrained() -> Any:
        return _FakePretrained(_FakeModel(list(profile.model_stems)))

    def fake_import_numpy() -> Any:
        return np

    monkeypatch.setattr(demucs_module, "_import_torch", fake_import_torch)
    monkeypatch.setattr(
        demucs_module, "_import_demucs_pretrained", fake_import_demucs_pretrained
    )
    monkeypatch.setattr(demucs_module, "_import_numpy", fake_import_numpy)

    # Inject a fake demucs package so `from demucs.apply import apply_model`
    # inside separate() resolves to our stub.
    fake_apply_module = types.ModuleType("demucs.apply")
    fake_apply_module.apply_model = fake_apply_model  # type: ignore[attr-defined]
    fake_demucs_module = types.ModuleType("demucs")
    fake_demucs_module.apply = fake_apply_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "demucs", fake_demucs_module)
    monkeypatch.setitem(sys.modules, "demucs.apply", fake_apply_module)

    # Fixture checkpoint carrying the real profile checksum prefix.
    checkpoint_dir = demucs_module._default_model_dir() / "hub" / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    payload = b"fake-weights"
    (checkpoint_dir / "955717e8-8726e21a.th").write_bytes(payload)

    def fake_sha256() -> Any:
        return _FakeHasher(profile.checkpoint_checksum)

    monkeypatch.setattr(demucs_module.hashlib, "sha256", fake_sha256)
    return captured


def test_separate_vocals_instrumental_with_stub_engine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    captured = _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    mixture[0, :10] = 0.5
    captured["mixture"] = mixture
    calls: list[float] = []
    stems = demucs_module.separate(
        mixture,
        profile,
        mode="vocals_instrumental",
        progress_callback=calls.append,
        cancellation_checker=lambda: False,
    )

    assert set(stems) == {"vocals", "instrumental"}
    # Mixture-minus-vocals policy: vocals + instrumental reconstructs the input.
    np.testing.assert_allclose(stems["vocals"] + stems["instrumental"], mixture, atol=1e-5)
    # Stage-boundary progress fired.
    assert calls == [0.0, 1.0]


def test_separate_rejects_unsupported_mode() -> None:
    import numpy as np

    profile = get_profile(DEFAULT_PROFILE_ID)
    with pytest.raises(SeparationError) as excinfo:
        demucs_module.separate(
            np.zeros((2, 100), dtype=np.float32), profile, mode="full_stems"
        )
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED


def test_separate_cancels_before_inference() -> None:
    import numpy as np

    profile = get_profile(DEFAULT_PROFILE_ID)
    with pytest.raises(SeparationError) as excinfo:
        demucs_module.separate(
            np.zeros((2, 100), dtype=np.float32),
            profile,
            mode="vocals_instrumental",
            cancellation_checker=lambda: True,
        )
    assert excinfo.value.code == ErrorCode.CANCELED


def test_checkpoint_checksum_mismatch_is_model_load_failed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    checkpoint_dir = demucs_module._default_model_dir() / "hub" / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint_dir / "955717e8-8726e21a.th"
    checkpoint.write_bytes(b"tampered-weights")

    # Real hashlib this time: the tampered payload must not match the prefix.
    monkeypatch.setattr(demucs_module, "_import_torch", lambda: _FakeTorch)
    monkeypatch.setattr(
        demucs_module,
        "_import_demucs_pretrained",
        lambda: _FakePretrained(_FakeModel(list(profile.model_stems))),
    )

    with pytest.raises(SeparationError) as excinfo:
        demucs_module.load_model(profile, device="cpu")
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED


def test_resolve_device_uses_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demucs_module, "_import_torch", lambda: _FakeTorch)
    assert demucs_module.resolve_device("cpu") == "cpu"
    assert demucs_module.resolve_device("auto") == "cpu"  # fake torch: no CUDA
    with pytest.raises(SeparationError):
        demucs_module.resolve_device("tpu")


def test_model_metadata_is_safe() -> None:
    metadata = demucs_module.model_metadata(get_profile(DEFAULT_PROFILE_ID))
    assert metadata["model_id"] == "htdemucs"
    assert metadata["sample_rate"] == 44100
    assert "absolute" not in str(metadata).lower()
    assert isinstance(metadata["model_stems"], list)


class _RealTorchLike:
    """torch stand-in exposing a load() whose calls the shim can capture."""

    version = "2.6.0"

    def __init__(self) -> None:
        self.load_calls: list[dict[str, Any]] = []

    def load(self, *args: Any, **kwargs: Any) -> Any:
        self.load_calls.append(kwargs)
        return {}


def _install_fake_torch_module(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    import types

    module = types.ModuleType("torch")
    module.__version__ = fake.version
    module.load = fake.load
    monkeypatch.setitem(sys.modules, "torch", module)


def test_weights_only_compat_forces_false_on_torch_2_6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """torch >= 2.6 defaults weights_only to True; the shim must force False."""
    fake = _RealTorchLike()
    fake.cuda = None  # the shim only touches torch.load
    _install_fake_torch_module(monkeypatch, fake)

    with demucs_module._weights_only_compat():
        import torch

        torch.load("checkpoint.th")

    assert len(fake.load_calls) == 1
    assert fake.load_calls[0].get("weights_only") is False


def test_weights_only_compat_restores_torch_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RealTorchLike()
    _install_fake_torch_module(monkeypatch, fake)

    with demucs_module._weights_only_compat():
        import torch

        torch.load("checkpoint.th")  # wrapped: forced weights_only=False

    import torch

    torch.load("checkpoint.th")  # restored: no forced kwarg

    assert len(fake.load_calls) == 2
    assert fake.load_calls[0].get("weights_only") is False
    assert "weights_only" not in fake.load_calls[1]


def test_weights_only_compat_skips_torch_below_2_6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On torch <= 2.5 the default is already False; the shim must not patch."""
    fake = _RealTorchLike()
    fake.version = "2.5.1"
    _install_fake_torch_module(monkeypatch, fake)

    with demucs_module._weights_only_compat():
        import torch

        torch.load("checkpoint.th")

    assert len(fake.load_calls) == 1
    assert "weights_only" not in fake.load_calls[0]


def test_weights_only_compat_survives_missing_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No torch installed: the shim is a no-op, not an ImportError."""
    monkeypatch.setitem(sys.modules, "torch", None)  # blocks `import torch`

    with demucs_module._weights_only_compat():
        pass  # must not raise
