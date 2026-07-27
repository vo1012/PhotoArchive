"""place_for_gps() rounds coordinates and caches by bucket, AND calls rg.search() with
mode=1 (REVIEW-HANDOFF.md, Раунд 42 [БЛОКЕР] 1, продолжение Раунда 41) -- the library's
default mode=2 respawns a multiprocessing.Process pool on every single call (measured
~130-800 мс/вызов on Windows spawn), mode=1 reuses the library's own cached (@singleton)
KD-tree in-process (measured ~0.01 мс/вызов) -- see place_for_gps()'s own docstring for the
full finding. mode=1 alone fixes the blocker for any route shape (including GPS points with
no spatial clustering, e.g. a hike/road trip), so the coordinate-rounding cache below is now
a minor bonus, not the critical defense -- kept and still tested because it's cheap and
doesn't hurt, but test_mode_1_is_always_used() is what actually guards the blocker fix."""
import pytest

import photosort_win as m


@pytest.fixture(autouse=True)
def _clear_cache():
    m._place_cache.clear()
    yield
    m._place_cache.clear()


def _fake_search(calls, city="Moscow", cc="RU"):
    def search(points, mode=2, verbose=False):
        calls.append((list(points), mode))
        return [{"name": city, "cc": cc}]
    return search


def test_mode_1_is_always_used(monkeypatch):
    """Раунд 42 [БЛОКЕР] 1: mode=2 (библиотечное умолчание) -- источник всей стоимости,
    не размер датасета. Если это когда-нибудь тихо откатят обратно на умолчание (например,
    уберут явный mode=1 при рефакторинге вызова) -- блокер вернётся молча, без падения
    остальных тестов этого файла (они проверяют только количество вызовов, не их аргументы).
    Эмпирическая проверка (не докстринг на слово) -- см. живое измерение в докстринге
    place_for_gps(): mode=1 в ~80000 раз быстрее на этой же машине."""
    import reverse_geocoder as rg
    calls = []
    monkeypatch.setattr(rg, "search", _fake_search(calls))

    m.place_for_gps(55.75, 37.65)

    assert len(calls) == 1
    _, mode_used = calls[0]
    assert mode_used == 1


def test_nearby_points_share_one_real_call(monkeypatch):
    import reverse_geocoder as rg
    calls = []
    monkeypatch.setattr(rg, "search", _fake_search(calls))

    p1 = m.place_for_gps(55.7501, 37.6501)
    p2 = m.place_for_gps(55.7502, 37.6499)  # rounds to the same (55.75, 37.65) bucket

    assert p1 == p2 == "Moscow"
    assert len(calls) == 1


def test_far_apart_points_each_get_a_real_call(monkeypatch):
    import reverse_geocoder as rg
    calls = []
    monkeypatch.setattr(rg, "search", _fake_search(calls))

    m.place_for_gps(55.75, 37.65)
    m.place_for_gps(10.00, 20.00)

    assert len(calls) == 2


def test_missing_city_result_is_cached_as_none(monkeypatch):
    import reverse_geocoder as rg
    calls = []
    monkeypatch.setattr(rg, "search", _fake_search(calls, city=None))

    p1 = m.place_for_gps(1.0, 2.0)
    p2 = m.place_for_gps(1.0, 2.0)

    assert p1 is None and p2 is None
    assert len(calls) == 1  # the None result itself was cached, not re-looked-up


def test_none_coordinates_never_call_search(monkeypatch):
    import reverse_geocoder as rg
    calls = []
    monkeypatch.setattr(rg, "search", _fake_search(calls))

    assert m.place_for_gps(None, None) is None
    assert calls == []


def test_exception_is_not_cached_and_retried_next_call(monkeypatch):
    import reverse_geocoder as rg
    calls = []

    def flaky_search(points, mode=2, verbose=False):
        calls.append(list(points))
        if len(calls) == 1:
            raise RuntimeError("lazy-load hiccup")
        return [{"name": "Moscow", "cc": "RU"}]

    monkeypatch.setattr(rg, "search", flaky_search)

    p1 = m.place_for_gps(55.75, 37.65)
    p2 = m.place_for_gps(55.75, 37.65)

    assert p1 is None
    assert p2 == "Moscow"
    assert len(calls) == 2  # the failed first call was not cached, so it was retried


def test_foreign_city_appends_country_code(monkeypatch):
    import reverse_geocoder as rg
    calls = []
    monkeypatch.setattr(rg, "search", _fake_search(calls, city="Paris", cc="FR"))

    assert m.place_for_gps(48.85, 2.35, home_country="RU") == "Paris, FR"


def test_mode_1_actually_wins_on_the_real_singleton():
    """REVIEW-HANDOFF.md, Раунд 43 [ЗАМЕЧАНИЕ]: test_mode_1_is_always_used() above mocks
    rg.search entirely, so it can only catch "the mode=1 argument disappeared from the call"
    -- it cannot catch "the argument is still there but the library ignores it", which is
    exactly what happens once reverse_geocoder's @singleton-decorated RGeocoder has already
    been constructed elsewhere in the process with a different mode (the decorator keys its
    cache by class only, not by mode -- confirmed by reading reverse_geocoder/__init__.py).
    This test calls the real (unmocked) library once and checks the real singleton's .mode
    after place_for_gps() runs -- the only way to see whether mode=1 actually took effect,
    not just whether it was passed."""
    import reverse_geocoder as rg

    place = m.place_for_gps(55.75, 37.65)

    assert place  # sanity: the real geocoder actually resolved a city, not an exception path
    assert rg.RGeocoder().mode == 1
