"""m.gui_font_scale() -- множитель размера шрифта GUI-мастера ПОВЕРХ системного «Масштаба»
Windows (photoarchive_config.yaml: gui_font_scale). Отдельно от load_yaml_config()/Config:
настройка интерфейса, не движка, потребитель -- только gui_menu ДО старта движка. См.
PROMPT_run_screen.md §7 и SESSION-HANDOFF-ARCHIVE.md запись 2026-08-31 (крупный шрифт).
"""
import pytest

import photosort_win as m


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "photoarchive_config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_missing_file_returns_default(tmp_path):
    assert m.gui_font_scale(str(tmp_path / "nope.yaml")) == m.GUI_FONT_SCALE_DEFAULT


def test_file_without_key_returns_default(tmp_path):
    path = _write(tmp_path, "bydate_granularity: year\n")
    assert m.gui_font_scale(path) == m.GUI_FONT_SCALE_DEFAULT


def test_empty_file_returns_default(tmp_path):
    assert m.gui_font_scale(_write(tmp_path, "")) == m.GUI_FONT_SCALE_DEFAULT


def test_valid_value_passes_through(tmp_path):
    assert m.gui_font_scale(_write(tmp_path, "gui_font_scale: 1.35\n")) == 1.35


def test_integer_value_accepted(tmp_path):
    assert m.gui_font_scale(_write(tmp_path, "gui_font_scale: 1\n")) == 1.0


@pytest.mark.parametrize("raw,expected", [
    ("0.5", m.GUI_FONT_SCALE_MIN),      # ниже пола -> клэмп к 1.0
    ("0", m.GUI_FONT_SCALE_MIN),
    ("-3", m.GUI_FONT_SCALE_MIN),
    ("9.9", m.GUI_FONT_SCALE_MAX),      # выше потолка -> клэмп к 1.5
    (".inf", m.GUI_FONT_SCALE_MAX),
    ("-.inf", m.GUI_FONT_SCALE_MIN),
])
def test_out_of_range_is_clamped(tmp_path, raw, expected):
    assert m.gui_font_scale(_write(tmp_path, f"gui_font_scale: {raw}\n")) == expected


@pytest.mark.parametrize("raw", ["large", "'1.2x'", "[1.2]", "{a: b}", ".nan"])
def test_non_numeric_or_nan_returns_default(tmp_path, raw):
    path = _write(tmp_path, f"gui_font_scale: {raw}\n")
    assert m.gui_font_scale(path) == m.GUI_FONT_SCALE_DEFAULT


def test_broken_yaml_returns_default(tmp_path):
    assert m.gui_font_scale(_write(tmp_path, "gui_font_scale: [unclosed\n")) == m.GUI_FONT_SCALE_DEFAULT


def test_top_level_not_a_mapping_returns_default(tmp_path):
    assert m.gui_font_scale(_write(tmp_path, "- just\n- a list\n")) == m.GUI_FONT_SCALE_DEFAULT


def test_load_yaml_config_does_not_warn_about_gui_font_scale(tmp_path):
    """gui_font_scale -- легальный ключ файла, но НЕ поле Config: load_yaml_config() не должен
    ни ругаться на него как на незнакомый, ни протаскивать его в возвращаемый набор (иначе
    Config(**overrides) упадёт с TypeError)."""
    path = _write(tmp_path, "gui_font_scale: 1.3\nbydate_granularity: day\n")
    warnings = []
    overrides = m.load_yaml_config(path, log=warnings.append)
    assert overrides == {"bydate_granularity": "day"}
    assert not any("gui_font_scale" in w for w in warnings)


def test_load_yaml_config_still_warns_about_truly_unknown_key(tmp_path):
    path = _write(tmp_path, "gui_font_scale: 1.3\nnonsense_key: 1\n")
    warnings = []
    m.load_yaml_config(path, log=warnings.append)
    assert any("nonsense_key" in w for w in warnings)
    assert not any("gui_font_scale" in w for w in warnings)
