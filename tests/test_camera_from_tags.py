"""camera_from_tags() / _tag_text() -- боевой прогон 2026-09-01: exiftool `-j` отдал Make/Model
как ЧИСЛО (int), (x or "").strip() уронил сканирование до отчёта с
`AttributeError: 'int' object has no attribute 'strip'`
(photosort_win.py:camera_from_tags <- analyze_batch <- run_analyze). Red-before-green:
кейсы с числовым тегом падали на прежней реализации."""
import photosort_win as m


class TestTagTextCoercion:
    def test_plain_string_stripped(self):
        assert m._tag_text("  Canon  ") == "Canon"

    def test_none_becomes_empty(self):
        assert m._tag_text(None) == ""

    def test_empty_and_zero_become_empty(self):
        # прежняя семантика `(x or "")`: 0 / "" / [] -- отсутствие значения, не "0"
        assert m._tag_text("") == ""
        assert m._tag_text(0) == ""
        assert m._tag_text(0.0) == ""
        assert m._tag_text([]) == ""

    def test_nonzero_int_coerced_to_str(self):
        assert m._tag_text(2020) == "2020"

    def test_float_coerced_to_str(self):
        assert m._tag_text(12.5) == "12.5"

    def test_list_does_not_raise(self):
        assert m._tag_text(["a", "b"])  # str(list) -- лишь бы не AttributeError


class TestCameraFromTags:
    def test_string_make_and_model(self):
        assert m.camera_from_tags({"Make": "Canon", "Model": "Canon EOS 5D"}) == "Canon EOS 5D"

    def test_make_not_in_model_concatenates(self):
        assert m.camera_from_tags({"Make": "NIKON", "Model": "D750"}) == "NIKON D750"

    def test_numeric_model_does_not_crash(self):
        # ровно боевой кейс: Model пришёл числом
        assert m.camera_from_tags({"Make": "SONY", "Model": 7}) == "SONY 7"

    def test_numeric_make_and_model_does_not_crash(self):
        assert m.camera_from_tags({"Make": 1, "Model": 2}) == "1 2"

    def test_zero_tags_return_none(self):
        assert m.camera_from_tags({"Make": 0, "Model": 0}) is None

    def test_missing_tags_return_none(self):
        assert m.camera_from_tags({}) is None
