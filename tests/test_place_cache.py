"""place_for_gps() rounds coordinates and caches by bucket (REVIEW-HANDOFF.md, Раунд 41
[БЛОКЕР] 1) -- rg.search() measured at ~130 мс/вызов necessarily unbatched (this pipeline
hashes/copies SOURCE items one at a time by design, see place_for_gps()'s own docstring for
why true cross-file batching isn't safe here); repeated calls for nearby points must not
re-pay that cost."""
import pytest

import photosort_win as m


@pytest.fixture(autouse=True)
def _clear_cache():
    m._place_cache.clear()
    yield
    m._place_cache.clear()


def _fake_search(calls, city="Moscow", cc="RU"):
    def search(points, verbose=False):
        calls.append(list(points))
        return [{"name": city, "cc": cc}]
    return search


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

    def flaky_search(points, verbose=False):
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
