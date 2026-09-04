import threading

from sector_vote.scheduler import run_periodic


class StopAfterOneWait:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = True
        return True


def test_periodic_runner_runs_immediately_then_waits():
    stop = StopAfterOneWait()
    calls = []

    run_periodic(lambda: calls.append("run"), 3600, stop)

    assert calls == ["run"]
    assert stop.waits == [3600]


def test_periodic_runner_contains_job_errors_and_keeps_schedule():
    stop = StopAfterOneWait()
    errors = []

    def fail():
        raise RuntimeError("boom")

    run_periodic(fail, 600, stop, on_error=lambda exc: errors.append(str(exc)))

    assert errors == ["boom"]
    assert stop.waits == [600]


def test_periodic_runner_does_not_run_after_stop():
    stop = threading.Event()
    stop.set()
    calls = []

    run_periodic(lambda: calls.append("run"), 10, stop)

    assert calls == []
