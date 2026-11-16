import pytest

from mysite.services.scoring import scale_lookup, scale_problems

TABLE = [(0, 0.0), (10, 30.0), (30, 60.0)]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0.0),
        (10, 30.0),  # exact table point
        (5, 15.0),  # halfway between 0 and 10
        (20, 45.0),  # halfway between 10 and 30
        (30, 60.0),
        (-3, 0.0),  # clamped below
        (99, 60.0),  # clamped above
    ],
)
def test_scale_lookup(raw, expected):
    assert scale_lookup(TABLE, raw) == expected


def test_scale_lookup_does_not_depend_on_point_order():
    assert scale_lookup(list(reversed(TABLE)), 20) == 45.0


def test_scale_lookup_rounds_to_one_decimal():
    assert scale_lookup([(0, 0), (3, 10)], 1) == 3.3


def test_scale_lookup_needs_points():
    with pytest.raises(ValueError):
        scale_lookup([], 1)


def test_scale_problems():
    assert scale_problems(TABLE, 30) == []
    assert scale_problems([(1, 5.0), (30, 60.0)], 30) == ["the table must start at raw 0"]
    assert scale_problems([(0, 0.0), (20, 60.0)], 30) == [
        "the table must end at raw 30 (number of questions)"
    ]
    assert scale_problems([(0, 10.0), (30, 5.0)], 30) == ["more correct answers cannot give fewer points"]
    assert len(scale_problems([], 30)) == 2
