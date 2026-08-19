from codex_proxy.backoff import Backoff, full_jitter


def test_ceiling_grows_exponentially_and_caps():
    b = Backoff(initial=1.0, multiplier=2.0, maximum=10.0, jitter=lambda x: x)
    assert b.ceiling(1) == 1.0
    assert b.ceiling(2) == 2.0
    assert b.ceiling(3) == 4.0
    assert b.ceiling(4) == 8.0
    assert b.ceiling(5) == 10.0  # 16 capped to 10
    assert b.ceiling(6) == 10.0


def test_attempt_below_one_is_clamped():
    b = Backoff(initial=1.0, multiplier=2.0, maximum=10.0, jitter=lambda x: x)
    assert b.ceiling(0) == 1.0


def test_delay_applies_injected_jitter():
    b = Backoff(initial=1.0, multiplier=2.0, maximum=10.0, jitter=lambda x: x / 2)
    assert b.delay(3) == 2.0  # ceiling 4 -> jitter halves it


def test_full_jitter_stays_within_bounds():
    for _ in range(200):
        v = full_jitter(5.0)
        assert 0.0 <= v <= 5.0
