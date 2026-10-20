from datetime import datetime, timezone

import numpy as np

from app.api.routes.search import parse
from app.vision.attributes import dominant_colour, is_greyscale, person_colours


def test_colour_names_from_regions():
    red = np.zeros((40, 40, 3), np.uint8); red[:] = (30, 30, 200)
    blue = np.zeros((40, 40, 3), np.uint8); blue[:] = (200, 60, 30)
    white = np.full((40, 40, 3), 235, np.uint8)
    assert dominant_colour(red) == "red"
    assert dominant_colour(blue) == "blue"
    assert dominant_colour(white) == "white"
    assert dominant_colour(white, frame_is_grey=True) is None  # IR night frame: no colour claimed


def test_ir_greyscale_frame_reports_no_colour():
    grey = np.full((40, 40, 3), 120, np.uint8)
    assert is_greyscale(grey)
    assert person_colours(grey, (0, 0, 1, 1)) == {}


def test_person_upper_and_lower_colours():
    frame = np.zeros((200, 100, 3), np.uint8)
    frame[:100] = (30, 30, 200)   # red top half
    frame[100:] = (200, 60, 30)   # blue bottom half
    c = person_colours(frame, (0.0, 0.0, 1.0, 1.0))
    assert c == {"upper": "red", "lower": "blue"}


def test_query_parsing():
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    f = parse("person in red shirt yesterday at the driveway", {"ramesh": 3}, {"gate": 1}, now)
    assert f["labels"] == {"person"} and f["colours"] == [("red", "upper")]
    assert f["since"].day == 19 and f["until"].day == 20 and f["rest"] == ["driveway"]
    g = parse("Ramesh gate night", {"ramesh": 3}, {"gate": 1}, now)
    assert g["people"] == {3} and g["cameras"] == {1} and g["hours"] == (21, 6)
    h = parse("blue car", {}, {}, now)
    assert h["labels"] == {"car"} and h["colours"] == [("blue", None)]
