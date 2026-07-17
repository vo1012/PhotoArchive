"""Config.__post_init__ -- pure validation/derived-path logic, no filesystem writes (paths are
only joined/normalized, never created). Migrated scenarios from
ci/windows_ci_test.py's test_negative_free_space_margin_and_numeric_config_validation (that
test's photoarchive_config.yaml-loading/CLI-plumbing portions stay in ci/, only the underlying
per-field validation rules are duplicated here as fast direct-construction checks)."""
import pytest

import photosort_win as m


def _make(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def test_valid_config_derives_expected_roots(tmp_path):
    cfg = _make(tmp_path)
    assert cfg.albums_root == str(tmp_path / "target" / "Albums")
    assert cfg.bydate_root == str(tmp_path / "target" / "ByDate")
    assert cfg.raw_root == str(tmp_path / "target" / "RAW")
    # PROTECTED dump names are always unioned in, even though the user never configured them.
    assert {"bydate", "albums", "raw", "_unsorted"} <= cfg.dump_segment_names_lower


@pytest.mark.parametrize("field,value", [
    ("bydate_granularity", "week"),
    ("raw_layout", "flat"),
    ("free_space_margin_gb", -1.0),
    ("max_archive_depth", 0),
    ("max_dest_path", 9),
    ("small_image_px", -1),
    ("sample_limit", -1),
    ("read_retry_count", -1),
    ("read_retry_delay", -0.1),
])
def test_invalid_field_values_raise(tmp_path, field, value):
    with pytest.raises(ValueError):
        _make(tmp_path, **{field: value})


def test_relative_source_rejected(tmp_path):
    with pytest.raises(ValueError):
        _make(tmp_path, source="relative/source")


def test_relative_target_rejected(tmp_path):
    with pytest.raises(ValueError):
        _make(tmp_path, target="relative/target")


def test_source_equals_target_rejected(tmp_path):
    same = str(tmp_path / "same")
    with pytest.raises(ValueError):
        _make(tmp_path, source=same, target=same)


def test_source_inside_target_rejected(tmp_path):
    target = str(tmp_path / "archive")
    source = str(tmp_path / "archive" / "nested_source")
    with pytest.raises(ValueError):
        _make(tmp_path, source=source, target=target)


def test_target_inside_source_is_allowed(tmp_path):
    # A supported, documented scenario (e.g. SOURCE=D:\, TARGET=D:\Архив фото) -- protected
    # against self-ingestion at walk time (SourceWalker skips TARGET entirely), not rejected
    # by Config validation the way the reverse (source inside target) is.
    source = str(tmp_path / "drive")
    target = str(tmp_path / "drive" / "archive")
    cfg = _make(tmp_path, source=source, target=target)
    assert cfg.target == target


def test_free_space_margin_gb_zero_is_allowed(tmp_path):
    # Boundary: only negative is rejected, 0 (no safety margin at all) is a valid, if risky,
    # user choice.
    cfg = _make(tmp_path, free_space_margin_gb=0.0)
    assert cfg.free_space_margin_gb == 0.0
