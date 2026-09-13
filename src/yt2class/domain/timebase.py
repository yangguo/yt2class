"""Source and derived-media timestamp mappings."""

from __future__ import annotations

from dataclasses import dataclass
import math


class TimeMappingError(ValueError):
    """Raised when a timestamp cannot be mapped within a clip."""


def _finite_nonnegative(value: float, label: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise TimeMappingError(f"{label} must be a finite non-negative number")
    return value


def _check_point(value: float, duration: float, label: str) -> float:
    value = _finite_nonnegative(value, label)
    # A tiny tolerance handles timestamps produced by rational PTS arithmetic while
    # still rejecting meaningful out-of-range values.
    if value > duration and value - duration > 1e-9:
        raise TimeMappingError(f"{label} {value} is outside [0, {duration}]")
    return min(value, duration)


@dataclass(frozen=True)
class TimeMapping:
    """Map local timestamps through nested clips back to source time.

    ``offset_seconds`` is relative to ``parent``.  ``speed_ratio`` describes
    playback speed in this mapping: one local second consumes
    ``speed_ratio`` seconds in the parent.  Therefore a clip from ``start`` to
    ``end`` has local duration ``(end - start) / speed_ratio``.
    """

    source_duration_seconds: float
    offset_seconds: float = 0.0
    duration_seconds: float | None = None
    speed_ratio: float = 1.0
    parent: "TimeMapping | None" = None

    def __post_init__(self) -> None:
        source_duration = _finite_nonnegative(
            self.source_duration_seconds, "source_duration_seconds"
        )
        if source_duration <= 0:
            raise TimeMappingError("source duration must be greater than zero")
        object.__setattr__(self, "source_duration_seconds", source_duration)

        offset = _finite_nonnegative(self.offset_seconds, "offset_seconds")
        speed = float(self.speed_ratio)
        if not math.isfinite(speed) or speed <= 0:
            raise TimeMappingError("speed_ratio must be finite and greater than zero")
        object.__setattr__(self, "offset_seconds", offset)
        object.__setattr__(self, "speed_ratio", speed)

        if self.parent is None:
            if offset != 0:
                raise TimeMappingError("root mapping offset must be zero")
            duration = source_duration if self.duration_seconds is None else float(self.duration_seconds)
            if abs(duration - source_duration) > 1e-9:
                raise TimeMappingError("root mapping duration must equal source duration")
            object.__setattr__(self, "duration_seconds", source_duration)
            return

        if abs(self.parent.source_duration_seconds - source_duration) > 1e-9:
            raise TimeMappingError("nested mappings must share the source duration")
        parent_duration = self.parent.duration_seconds
        if offset > parent_duration + 1e-9:
            raise TimeMappingError("clip offset is outside the parent mapping")
        if self.duration_seconds is None:
            raise TimeMappingError("nested mapping requires a duration")
        duration = float(self.duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise TimeMappingError("mapping duration must be finite and greater than zero")
        consumed_parent = duration * speed
        if offset + consumed_parent > parent_duration + 1e-9:
            raise TimeMappingError("clip range is outside the parent mapping")
        object.__setattr__(self, "duration_seconds", duration)

    @classmethod
    def identity(cls, source_duration_seconds: float) -> "TimeMapping":
        """Create a root mapping covering the complete source."""

        return cls(source_duration_seconds=float(source_duration_seconds))

    def clip(self, start_seconds: float, end_seconds: float, *, speed_ratio: float = 1.0) -> "TimeMapping":
        """Return a mapping for a parent-relative clip, optionally retimed."""

        start = _finite_nonnegative(start_seconds, "clip start")
        end = _finite_nonnegative(end_seconds, "clip end")
        if not start < end:
            raise TimeMappingError("clip start must be less than clip end")
        if end > self.duration_seconds + 1e-9:
            raise TimeMappingError("clip range is outside the parent mapping")
        speed = float(speed_ratio)
        if not math.isfinite(speed) or speed <= 0:
            raise TimeMappingError("speed_ratio must be finite and greater than zero")
        return type(self)(
            source_duration_seconds=self.source_duration_seconds,
            offset_seconds=min(start, self.duration_seconds),
            duration_seconds=(end - start) / speed,
            speed_ratio=speed,
            parent=self,
        )

    def to_source(self, local_timestamp: float) -> float:
        """Map one local timestamp to the original source timeline."""

        local = _check_point(local_timestamp, self.duration_seconds, "local timestamp")
        parent_timestamp = self.offset_seconds + local * self.speed_ratio
        if self.parent is None:
            return min(parent_timestamp, self.source_duration_seconds)
        return self.parent.to_source(parent_timestamp)

    def from_source(self, source_timestamp: float) -> float:
        """Map an in-range source timestamp into this clip's local timeline."""

        source = _check_point(source_timestamp, self.source_duration_seconds, "source timestamp")
        if self.parent is None:
            return source
        parent_timestamp = self.parent.from_source(source)
        start = self.offset_seconds
        end = start + self.duration_seconds * self.speed_ratio
        if parent_timestamp < start - 1e-9 or parent_timestamp > end + 1e-9:
            raise TimeMappingError("source timestamp is outside this clip")
        local = (min(max(parent_timestamp, start), end) - start) / self.speed_ratio
        return min(local, self.duration_seconds)

    def map_range(self, start_seconds: float, end_seconds: float) -> tuple[float, float]:
        """Map a half-open local range to source endpoints."""

        start = _finite_nonnegative(start_seconds, "range start")
        end = _finite_nonnegative(end_seconds, "range end")
        if not start < end:
            raise TimeMappingError("range start must be less than range end")
        if end > self.duration_seconds + 1e-9:
            raise TimeMappingError("range is outside this mapping")
        return self.to_source(start), self.to_source(end)


__all__ = ["TimeMapping", "TimeMappingError"]
