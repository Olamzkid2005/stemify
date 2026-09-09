"""Phase B adapter tests: the pure pieces of the drumsep wrapper.

The source-name mapping is the only place a silent mistake would produce
wrongly-labeled audio, so it is locked here. Full inference requires the
checkpoint (reference machine) and is covered by the manual quality pass.
"""

from __future__ import annotations

import pytest

from worker.errors import ErrorCode
from worker.models.base import SeparationError
from worker.models.drumsep import _source_key_order
from worker.models.profiles import DRUMSEP_PROFILE


def test_source_names_map_by_documented_names() -> None:
    order = _source_key_order(["kick", "snare", "cymbals", "toms"])
    assert order == ["drums_kick", "drums_snare", "drums_cymbals", "drums_toms"]


def test_source_names_map_spanish_aliases() -> None:
    order = _source_key_order(["bombo", "redoblante", "platillos", "toms"])
    assert order == ["drums_kick", "drums_snare", "drums_cymbals", "drums_toms"]


def test_unrecognized_sources_fall_back_to_documented_order() -> None:
    order = _source_key_order(["a", "b", "c", "d"])
    assert order == list(DRUMSEP_PROFILE.model_stems)


def test_wrong_source_count_is_rejected() -> None:
    with pytest.raises(SeparationError) as excinfo:
        _source_key_order(["kick", "snare"])
    assert excinfo.value.code == ErrorCode.INFERENCE_FAILED
