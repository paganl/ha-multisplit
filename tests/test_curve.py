"""The curve is the shape of a measured thing, so its edges matter."""

import pytest

from core.model import Curve


def test_interpolates_linearly_between_breakpoints():
    curve = Curve(((0.0, 0.3), (7.0, 0.0), (14.0, -0.4)))
    assert curve.at(0.0) == pytest.approx(0.3)
    assert curve.at(3.5) == pytest.approx(0.15)
    assert curve.at(7.0) == pytest.approx(0.0)
    assert curve.at(10.5) == pytest.approx(-0.2)
    assert curve.at(14.0) == pytest.approx(-0.4)


def test_clamps_rather_than_extrapolating():
    """Extrapolating a measured curve into unobserved weather invents data."""
    curve = Curve(((0.0, 0.3), (14.0, -0.4)))
    assert curve.at(-15.0) == pytest.approx(0.3)
    assert curve.at(40.0) == pytest.approx(-0.4)


def test_unknown_outdoor_takes_the_coldest_breakpoint():
    """The cautious end: smallest cut-off, shortest rest."""
    curve = Curve(((0.0, 0.3), (14.0, -0.4)))
    assert curve.at(None) == pytest.approx(0.3)


def test_constant_curve_ignores_its_input():
    curve = Curve.constant(0.5)
    assert curve.at(-10.0) == curve.at(30.0) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "points",
    [
        (),
        ((7.0, 1.0), (0.0, 2.0)),  # not sorted
        ((7.0, 1.0), (7.0, 2.0)),  # duplicate x
    ],
)
def test_rejects_malformed_curves(points):
    with pytest.raises(ValueError):
        Curve(points)
