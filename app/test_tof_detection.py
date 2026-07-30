"""Pure detection-helper tests (no hardware required)."""

from tof_sensor import (
    collect_baseline_from_samples,
    detect_drop_from_series,
    is_sudden_decrease,
    median_cm,
    update_consecutive_hits,
)


def test_median_cm():
    assert median_cm([10.0, 20.0, 30.0]) == 20.0


def test_collect_baseline_ignores_invalid():
    baseline = collect_baseline_from_samples(
        [None, 40.0, None, 42.0, 41.0],
        min_samples=3,
    )
    assert baseline == 41.0


def test_collect_baseline_requires_min_samples():
    assert collect_baseline_from_samples([40.0, None], min_samples=3) is None


def test_sudden_decrease():
    assert is_sudden_decrease(40.0, 35.0, 4.0) is True
    assert is_sudden_decrease(40.0, 37.0, 4.0) is False


def test_detect_drop_from_series():
    detected, min_d, drop = detect_drop_from_series(
        baseline_cm=40.0,
        readings=[40.0, 39.0, 30.0, 29.0, 28.0],
        threshold_cm=4.0,
        consecutive_hits=2,
    )
    assert detected is True
    assert min_d == 29.0
    assert drop == 11.0


def test_noise_does_not_trigger():
    detected, _, _ = detect_drop_from_series(
        baseline_cm=40.0,
        readings=[40.0, 35.0, 40.0, 35.0, 40.0],
        threshold_cm=4.0,
        consecutive_hits=2,
    )
    assert detected is False


def test_invalid_resets_consecutive():
    assert update_consecutive_hits(1, 40.0, None, 4.0) == 0


if __name__ == "__main__":
    test_median_cm()
    test_collect_baseline_ignores_invalid()
    test_collect_baseline_requires_min_samples()
    test_sudden_decrease()
    test_detect_drop_from_series()
    test_noise_does_not_trigger()
    test_invalid_resets_consecutive()
    print("All ToF detection tests passed.")
