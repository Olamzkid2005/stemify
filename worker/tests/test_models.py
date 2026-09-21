"""Task 9 tests: profile allowlisting and the adapter contract.

Profile tests are pure Python (no torch/demucs needed). Adapter behaviour is
tested by monkeypatching the lazy importers with fakes, so no model download,
GPU, or numpy stack is required. The real engine path is covered later by
scheduled fixture runs (plan Section 17.3).
"""

from __future__ import annotations

import sys
from pathlib import Path
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
    # The default 4-stem checkpoint serves both whole-track modes; the
    # experimental 6-stem model is no longer allowlisted.
    assert profile.supported_modes == ("vocals_instrumental", "full_stems")


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(SeparationError) as excinfo:
        get_profile("not_a_profile")
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED


def test_model_dir_follows_the_documented_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """STEMIFY_MODEL_DIR is honoured (it is documented in five places).

    It used to be read by nothing, so an operator pointing the cache elsewhere
    silently got the default directory and an 84MB re-download.
    """
    absolute = tmp_path / "elsewhere"
    monkeypatch.setenv("STEMIFY_MODEL_DIR", str(absolute))
    assert demucs_module._default_model_dir() == absolute

    # A relative value resolves against the repository root, not the process cwd:
    # the launcher runs the worker from worker/, so `.env.example`'s
    # ./data/models would otherwise mean worker/data/models.
    monkeypatch.setenv("STEMIFY_MODEL_DIR", "./data/models")
    assert demucs_module._default_model_dir() == (
        demucs_module._REPO_ROOT / "data" / "models"
    ).resolve()


def test_model_dir_falls_back_to_the_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("STEMIFY_MODEL_DIR", raising=False)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    assert demucs_module._default_model_dir() == tmp_path / "data" / "models"

    # Blank is not an override: it means "unset" (start.sh and .env examples use
    # an empty value to mean "leave the default alone").
    monkeypatch.setenv("STEMIFY_MODEL_DIR", "   ")
    assert demucs_module._default_model_dir() == tmp_path / "data" / "models"


def test_mode_resolves_profile_and_explicit_override_wins() -> None:
    """Both whole-track modes use the default 4-stem profile, drum_breakdown
    uses drumsep, and an explicit STEMIFY_MODEL_PROFILE that supports the mode
    still wins."""
    from worker.models.profiles import get_profile_for_mode

    assert get_profile_for_mode("full_stems").profile_id == "demucs_default"
    assert get_profile_for_mode("vocals_instrumental").profile_id == "demucs_default"
    assert get_profile_for_mode("drum_breakdown").profile_id == "drumsep"

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("STEMIFY_MODEL_PROFILE", "drumsep")
        # drumsep does not serve whole-track modes, so the mode's own profile wins.
        assert get_profile_for_mode("vocals_instrumental").profile_id == "demucs_default"
        assert get_profile_for_mode("drum_breakdown").profile_id == "drumsep"
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
        captured.update(kwargs)
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


def test_separate_full_stems_returns_three_non_overlapping_stems(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """full_stems = drums, bass and the residual instrumental bed."""
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    captured = _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    mixture[0, :10] = 0.5
    captured["mixture"] = mixture
    stems = demucs_module.separate(mixture, profile, mode="full_stems")

    assert set(stems) == {"drums", "bass", "instrumental"}
    # The stub returns the mixture for every model source, so each output is
    # exactly that and the residual is mixture - vocals - drums - bass. This
    # pins the adapter to deriving the bed from the other stems rather than
    # copying a model output, and keeps all three outputs full length.
    np.testing.assert_allclose(stems["drums"], mixture, atol=1e-6)
    np.testing.assert_allclose(stems["bass"], mixture, atol=1e-6)
    np.testing.assert_allclose(stems["instrumental"], -2.0 * mixture, atol=1e-6)


def test_separate_custom_returns_exactly_the_ticked_stems(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mode='custom' saves exactly the selection: model stems as-is, and the
    instrumental residual of the ticked sources only (docs/STEM_SELECTION_PLAN.md).

    With the stub returning the mixture per source, picking vocals+drums must
    give instrumental = mixture - vocals - drums = -1x mixture, and nothing
    else in the dict.
    """
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    captured = _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    mixture[0, :10] = 0.5
    captured["mixture"] = mixture
    stems = demucs_module.separate(
        mixture,
        profile,
        mode="custom",
        stem_selection=("vocals", "drums", "instrumental"),
    )

    assert set(stems) == {"vocals", "drums", "instrumental"}
    np.testing.assert_allclose(stems["vocals"], mixture, atol=1e-6)
    np.testing.assert_allclose(stems["drums"], mixture, atol=1e-6)
    np.testing.assert_allclose(stems["instrumental"], -1.0 * mixture, atol=1e-6)


def test_separate_custom_without_instrumental_saves_no_residual(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly what was ticked: no instrumental tick, no residual bounce."""
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    captured = _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    captured["mixture"] = mixture
    stems = demucs_module.separate(
        mixture, profile, mode="custom", stem_selection=("bass",)
    )
    assert set(stems) == {"bass"}


def test_separate_custom_refuses_missing_or_unknown_selection(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selection outside the allowlist (or absent) fails loudly at the
    adapter, before any inference happens."""
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    with pytest.raises(SeparationError):
        demucs_module.separate(mixture, profile, mode="custom")
    with pytest.raises(SeparationError):
        demucs_module.separate(
            mixture, profile, mode="custom", stem_selection=("piano",)
        )


def test_resolve_quality_presets_and_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STEMIFY_QUALITY resolves case-insensitively; an unknown value fails loudly."""
    from worker.models.profiles import resolve_quality

    monkeypatch.delenv("STEMIFY_QUALITY", raising=False)
    assert resolve_quality() == ("balanced", 0.4, 2)
    for name, overlap, shifts in (("fast", 0.25, 0), ("balanced", 0.4, 2)):
        monkeypatch.setenv("STEMIFY_QUALITY", name.upper())
        assert resolve_quality() == (name, overlap, shifts)
    # The retired "best" preset must fail loudly rather than quietly run at
    # some other quality (the strictness is deliberate).
    monkeypatch.setenv("STEMIFY_QUALITY", "best")
    with pytest.raises(SeparationError) as removed:
        resolve_quality()
    assert removed.value.code == ErrorCode.MODEL_LOAD_FAILED
    monkeypatch.setenv("STEMIFY_QUALITY", "insane")
    with pytest.raises(SeparationError) as excinfo:
        resolve_quality()
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED


def test_explicit_job_quality_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-job preset (DB column) wins over the STEMIFY_QUALITY env default."""
    from worker.models.profiles import resolve_quality

    monkeypatch.setenv("STEMIFY_QUALITY", "fast")
    assert resolve_quality("balanced") == ("balanced", 0.4, 2)
    # None falls back to the env var.
    assert resolve_quality(None) == ("fast", 0.25, 0)


def test_quality_preset_reaches_inference(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STEMIFY_QUALITY=fast lowers shifts/overlap at the apply_model call."""
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STEMIFY_QUALITY", "fast")
    captured = _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    captured["mixture"] = mixture
    demucs_module.separate(mixture, profile, mode="vocals_instrumental")

    assert captured["shifts"] == 0
    assert captured["overlap"] == 0.25


def test_quality_defaults_to_balanced_at_inference(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without STEMIFY_QUALITY, inference uses the balanced pinned settings."""
    import numpy as np

    _clear_model_cache()
    profile = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STEMIFY_QUALITY", raising=False)
    captured = _install_fake_engine(monkeypatch, profile)

    mixture = np.zeros((2, 100), dtype=np.float32)
    captured["mixture"] = mixture
    demucs_module.separate(mixture, profile, mode="vocals_instrumental")

    assert captured["shifts"] == 2
    assert captured["overlap"] == 0.4


def test_separate_rejects_unsupported_mode() -> None:
    import numpy as np

    profile = get_profile(DEFAULT_PROFILE_ID)
    with pytest.raises(SeparationError) as excinfo:
        demucs_module.separate(
            np.zeros((2, 100), dtype=np.float32), profile, mode="karaoke"
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


def test_weights_only_compat_skips_torch_without_a_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A torch-compatible minimal runtime without metadata must remain usable."""
    import types

    module = types.ModuleType("torch")
    module.load = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", module)

    with demucs_module._weights_only_compat():
        pass


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


# ---------------------------------------------------------------------------
# Chunked inference with per-chunk progress (the bar used to park at one value
# for the whole separation). These pin the pieces the replication depends on.
# ---------------------------------------------------------------------------


class _SubModel:
    """HTDemucs-like: a concrete model that does carry a segment."""

    def __init__(self, segment: float) -> None:
        self.segment = segment


class _BagModel:
    """BagOfModels-like: samplerate/sources and sub-models, but no `segment`."""

    def __init__(self, sub_segments: list[float], samplerate: int = 44100) -> None:
        self.models = [_SubModel(value) for value in sub_segments]
        self.samplerate = samplerate
        self.sources = ["drums", "bass", "other", "vocals"]


def test_chunk_offsets_match_demucs_stride() -> None:
    """Offsets are range(0, length, int((1 - overlap) * segment_length))."""
    # stride = int(0.75 * 400) = 300
    assert demucs_module._chunk_offsets(1000, 400, 0.25) == [0, 300, 600, 900]
    # stride = int(0.6 * 400) = 240
    assert demucs_module._chunk_offsets(1000, 400, 0.4) == [0, 240, 480, 720, 960]
    # Shorter than one chunk: a single pass over the whole signal.
    assert demucs_module._chunk_offsets(100, 400, 0.25) == [0]


def test_resolve_segment_seconds_prefers_the_profile_value() -> None:
    """An explicit profile chunk_length_seconds wins over the model's own."""
    assert demucs_module._resolve_segment_seconds(_SubModel(7.8), 5.0) == 5.0
    assert demucs_module._resolve_segment_seconds(_BagModel([7.8]), 5.0) == 5.0


def test_resolve_segment_seconds_uses_the_model_default() -> None:
    assert demucs_module._resolve_segment_seconds(_SubModel(6.0), None) == 6.0


def test_resolve_segment_seconds_reads_sub_models_for_a_bag() -> None:
    """A bag has no `.segment`; the effective chunk length is the bag minimum.

    The htdemucs checkpoint loads as a BagOfModels wrapping HTDemucs, and the
    profiles pin chunk_length_seconds=None, so this is the production path.
    Reading `model.segment` directly raised AttributeError and failed the job.
    """
    bag = _BagModel([7.8])
    assert not hasattr(bag, "segment")
    assert demucs_module._resolve_segment_seconds(bag, None) == 7.8
    assert demucs_module._resolve_segment_seconds(_BagModel([6.0, 4.0]), None) == 4.0


def test_resolve_segment_seconds_fails_loudly_when_unknown() -> None:
    """No pinned value and no discoverable segment: typed failure, not a guess."""

    class _Opaque:
        def __init__(self) -> None:
            self.models: list[Any] = []

    with pytest.raises(SeparationError) as excinfo:
        demucs_module._resolve_segment_seconds(_Opaque(), None)
    assert excinfo.value.code == ErrorCode.INFERENCE_FAILED


def test_drift_guard_accepts_the_installed_demucs() -> None:
    """The replication must keep matching the installed demucs.

    If demucs changes its chunk math, this fails loudly here instead of the
    progress bar silently reverting to two steps (the guard then falls back to
    apply_model by design). Skipped when the model stack is not installed.
    """
    pytest.importorskip("demucs.apply")
    assert demucs_module._apply_path_matches_installed() is True


def test_drift_guard_rejects_unknown_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A demucs whose split math we do not recognize must fall back, not run."""
    import types

    def apply_model(*args: Any, **kwargs: Any) -> Any:  # unrecognized implementation
        return None

    fake_apply = types.ModuleType("demucs.apply")
    fake_apply.apply_model = apply_model  # type: ignore[attr-defined]
    # Replace the package too: `import demucs.apply as x` resolves through the
    # parent package's cached attribute, so patching only the submodule key can
    # still hand back the real module once it has been imported.
    fake_package = types.ModuleType("demucs")
    fake_package.apply = fake_apply  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "demucs", fake_package)
    monkeypatch.setitem(sys.modules, "demucs.apply", fake_apply)
    assert demucs_module._apply_path_matches_installed() is False
