from datetime import time

from brief import DELIVERY_CATCHUP_END, _delivery_exit_code, _in_window
from checkin import _result_exit_code


def test_late_wake_still_delivers_the_current_day() -> None:
    assert _in_window(time(12, 9), time(5, 30), DELIVERY_CATCHUP_END)
    assert not _in_window(time(21, 0), time(5, 30), DELIVERY_CATCHUP_END)
    assert _in_window(time(23, 0), time(21, 40), time(3, 0))
    assert _in_window(time(2, 0), time(21, 40), time(3, 0))


def test_delivery_failure_requests_a_scheduler_retry() -> None:
    assert _delivery_exit_code("sent") == 0
    assert _delivery_exit_code("skipped") == 0
    assert _delivery_exit_code("uncertain") == 75
    assert _delivery_exit_code("failed") == 1


def test_checkin_send_failure_requests_a_scheduler_retry() -> None:
    assert _result_exit_code("SENT") == 0
    assert _result_exit_code("NO_REPLIES") == 0
    assert _result_exit_code("SEND_FAILED") == 1
