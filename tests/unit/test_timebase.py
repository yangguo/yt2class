from __future__ import annotations

import pytest

from yt2class.domain.timebase import TimeMapping, TimeMappingError


def test_identity_mapping_preserves_source_timestamps_and_boundaries():
    mapping = TimeMapping.identity(60.0)

    assert mapping.to_source(0.0) == 0.0
    assert mapping.to_source(60.0) == 60.0
    assert mapping.from_source(12.5) == 12.5
    assert mapping.map_range(0.0, 60.0) == (0.0, 60.0)


def test_nested_clip_mapping_and_inverse_round_trip():
    root = TimeMapping.identity(120.0)
    clip = root.clip(10.0, 50.0)
    nested = clip.clip(5.0, 20.0, speed_ratio=2.0)

    assert nested.to_source(0.0) == 15.0
    assert nested.to_source(7.5) == 30.0
    assert nested.to_source(nested.duration_seconds) == 30.0
    assert nested.from_source(30.0) == pytest.approx(7.5)
    assert nested.map_range(2.0, 4.0) == (19.0, 23.0)


@pytest.mark.parametrize("local_timestamp", [-0.001, 20.001])
def test_mapping_rejects_timestamps_outside_clip(local_timestamp: float):
    mapping = TimeMapping.identity(20.0).clip(4.0, 10.0)
    with pytest.raises(TimeMappingError):
        mapping.to_source(local_timestamp)


def test_mapping_rejects_invalid_ranges_and_preserves_source_bounds():
    mapping = TimeMapping.identity(20.0).clip(4.0, 10.0)
    with pytest.raises(TimeMappingError):
        mapping.map_range(5.0, 5.0)
    with pytest.raises(TimeMappingError):
        mapping.from_source(3.999)
    with pytest.raises(TimeMappingError):
        TimeMapping.identity(20.0).clip(15.0, 21.0)


def test_mapping_property_like_round_trips_for_nested_samples():
    mapping = TimeMapping.identity(100.0).clip(10.0, 80.0).clip(3.0, 20.0, speed_ratio=1.25)
    for local_timestamp in [0.0, 0.1, 1.0, 5.5, 10.0, mapping.duration_seconds]:
        source_timestamp = mapping.to_source(local_timestamp)
        assert mapping.from_source(source_timestamp) == pytest.approx(local_timestamp)
