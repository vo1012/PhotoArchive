"""gps_from_tags() -- (0.0, 0.0) ("Null Island") must be treated as absent GPS, not a real
coordinate (SESSION-HANDOFF.txt, 2026-07-31, пункт H): exiftool sometimes reports a
missing/blank GPS tag as literal 0/0, and reverse_geocoder happily resolves that to the
nearest real coastal city (found live: "Takoradi, GH"), not "no location"."""
import photosort_win as m


def test_null_island_is_treated_as_no_gps():
    assert m.gps_from_tags({"GPSLatitude": 0.0, "GPSLongitude": 0.0}) == (None, None)


def test_null_island_as_strings_is_treated_as_no_gps():
    assert m.gps_from_tags({"GPSLatitude": "0", "GPSLongitude": "0"}) == (None, None)


def test_real_coordinates_near_zero_are_kept():
    assert m.gps_from_tags({"GPSLatitude": 0.0, "GPSLongitude": 12.5}) == (0.0, 12.5)
    assert m.gps_from_tags({"GPSLatitude": -33.8688, "GPSLongitude": 151.2093}) == (
        -33.8688, 151.2093)


def test_missing_tags_are_none():
    assert m.gps_from_tags({}) == (None, None)
    assert m.gps_from_tags({"GPSLatitude": 1.0}) == (None, None)


def test_unparseable_tags_are_none():
    assert m.gps_from_tags({"GPSLatitude": "n/a", "GPSLongitude": "n/a"}) == (None, None)
