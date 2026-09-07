"""Result packaging (plan Task 10 / Section 12.7).

Builds the final job artifacts from validated per-stem outputs:
  - one manifest.json matching packages/contracts output-manifest.schema.json
  - one ZIP containing the selected-format stems plus the manifest

The manifest is validated against the shared schema before the ZIP is written,
and only complete artifacts are returned to the caller (plan 12.7: a partial
output must never make a job look completed).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from worker.encoding import FORMAT_EXTENSIONS, OutputError
from worker.errors import ErrorCode

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "contracts"
    / "schemas"
    / "output-manifest.schema.json"
)

MANIFEST_NAME = "manifest.json"
ZIP_MIME_TYPE = "application/zip"
ARCHIVE_STEM_KEY = "archive"  # reserved job_outputs.stem_key for the ZIP

# Human-readable labels for the stem list (plan Section 7.4).
STEM_LABELS: dict[str, str] = {
    "vocals": "Vocals",
    "instrumental": "Instrumental",
    "drums": "Drums",
    "bass": "Bass",
    "other": "Other",
}


def build_manifest(
    job_id: str,
    source_display_name: str,
    source_duration_seconds: float,
    source_sample_rate: int,
    source_channels: int,
    mode: str,
    output_format: str,
    stems: list[dict[str, Any]],
    model_id: str,
    model_revision: str,
    mixture_consistency: bool,
) -> dict[str, Any]:
    """Build a manifest dict matching output-manifest.schema.json exactly.

    stems: [{"name": stem_key, "durationSeconds": float, "sha256": hex}, ...]
    """
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "jobId": job_id,
        "source": {
            "displayName": source_display_name,
            "durationSeconds": round(source_duration_seconds, 3),
            "sampleRate": source_sample_rate,
            "channels": source_channels,
        },
        "separation": {
            "mode": mode,
            "modelId": model_id,
            "modelRevision": model_revision,
            "mixtureConsistency": mixture_consistency,
        },
        "output": {
            "format": output_format,
            "stems": [
                {
                    "name": stem["name"],
                    "durationSeconds": round(float(stem["durationSeconds"]), 3),
                    "sha256": stem["sha256"],
                }
                for stem in stems
            ],
        },
    }
    _validate_manifest(manifest)
    return manifest


def write_manifest(manifest: dict[str, Any], dest: Path) -> None:
    """Write manifest.json deterministically (sorted keys, stable newline)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_zip(stem_files: list[tuple[str, Path]], manifest: dict[str, Any], dest: Path) -> None:
    """Create the results ZIP: one file per stem plus manifest.json.

    stem_files: ordered [(stem_key, encoded_file), ...]. The expected file
    extension comes from the manifest's output format, never from the files
    themselves. All entry timestamps are zeroed for deterministic archives
    (plan acceptance: "ZIP contents are deterministic").
    """
    output_format = manifest.get("output", {}).get("format")
    expected_extension = FORMAT_EXTENSIONS.get(output_format) if output_format else None
    if expected_extension is None:
        raise OutputError(
            ErrorCode.OUTPUT_VALIDATION_FAILED,
            f"manifest output format {output_format!r} is not encodable",
        )

    if dest.exists():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for stem_key, path in stem_files:
                if path.suffix.lower() != f".{expected_extension}":
                    raise OutputError(
                        ErrorCode.OUTPUT_VALIDATION_FAILED,
                        f"stem file {path.name} does not match the selected format",
                    )
                entry = zipfile.ZipInfo(
                    f"{stem_key}.{expected_extension}", date_time=(1980, 1, 1, 0, 0, 0)
                )
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                with path.open("rb") as source, archive.open(entry, "w") as target:
                    shutil.copyfileobj(source, target, length=2**20)
            archive.writestr(
                zipfile.ZipInfo(MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0)),
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )
    except OutputError:
        # Never leave a partial archive behind (plan 12.7).
        if dest.exists():
            dest.unlink()
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if dest.exists():
            dest.unlink()
        raise OutputError(
            ErrorCode.OUTPUT_ENCODING_FAILED, f"could not write results archive: {error}"
        ) from error


def _validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate the manifest against the shared JSON schema.

    Uses jsonschema (with a registry for the common.schema.json $refs) when
    available; without it, enforces the required keys and patterns locally so
    the builder never emits a manifest the web would reject.
    """
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        _validate_manifest_minimally(manifest)
        return

    schemas_dir = SCHEMA_PATH.parent
    resources = []
    for name in ("common.schema.json", SCHEMA_PATH.name):
        schema = json.loads((schemas_dir / name).read_text(encoding="utf-8"))
        resources.append(
            (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
        )
    validator = Draft202012Validator(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
        registry=Registry().with_resources(resources),
    )
    errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "(root)"
        raise OutputError(
            ErrorCode.OUTPUT_VALIDATION_FAILED,
            f"manifest invalid at {location}: {first.message}",
        )


def _validate_manifest_minimally(manifest: dict[str, Any]) -> None:
    import re

    if manifest.get("schemaVersion") != 1:
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest schemaVersion must be 1")
    if not re.fullmatch(r"job_[A-Za-z0-9]{16,64}", str(manifest.get("jobId", ""))):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest jobId is malformed")
    source = manifest.get("source") or {}
    separation = manifest.get("separation") or {}
    output = manifest.get("output") or {}
    if not source.get("displayName"):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest source.displayName missing")
    if not (0 < float(source.get("durationSeconds", 0)) <= 3600):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest source.durationSeconds invalid")
    if source.get("sampleRate") not in (22050, 24000, 32000, 44100, 48000):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest source.sampleRate invalid")
    if source.get("channels") not in (1, 2):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest source.channels invalid")
    if separation.get("mode") not in ("vocals_instrumental", "full_stems"):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest separation.mode invalid")
    if not separation.get("modelId") or not separation.get("modelRevision"):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest model identity missing")
    if not isinstance(separation.get("mixtureConsistency"), bool):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest mixtureConsistency must be boolean")
    if output.get("format") not in ("mp3", "wav", "flac", "ogg", "m4a"):
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest output.format invalid")
    stems = output.get("stems") or []
    if not 1 <= len(stems) <= 4:
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "manifest stems must contain 1-4 entries")
    allowed_stems = {"vocals", "instrumental", "drums", "bass", "other"}
    for stem in stems:
        if stem.get("name") not in allowed_stems:
            raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, f"manifest stem {stem.get('name')!r} invalid")
        if not (0 < float(stem.get("durationSeconds", 0)) <= 3600):
            raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, f"manifest stem {stem.get('name')!r} duration invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", str(stem.get("sha256", ""))):
            raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, f"manifest stem {stem.get('name')!r} sha256 invalid")
