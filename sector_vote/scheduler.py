"""Small, dependency-free periodic job runner for the single-process web service."""

from collections.abc import Callable


def run_periodic(
    job: Callable[[], None],
    interval_seconds: float,
    stop_event,
    *,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run once at startup, then after each interval until stopped."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    while not stop_event.is_set():
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - a scheduler must survive one failed tick
            if on_error:
                on_error(exc)
        stop_event.wait(interval_seconds)
