"""Engine failure classification.

Stable public error codes are mapped to user-facing messages by the web app
(Section 32.16 of the product plan). The worker only emits these codes.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_AUDIO = "INVALID_AUDIO"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    GPU_OUT_OF_MEMORY = "GPU_OUT_OF_MEMORY"
    INFERENCE_FAILED = "INFERENCE_FAILED"
    OUTPUT_ENCODING_FAILED = "OUTPUT_ENCODING_FAILED"
    OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"
    STORAGE_UPLOAD_FAILED = "STORAGE_UPLOAD_FAILED"
    CANCELED = "CANCELED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
