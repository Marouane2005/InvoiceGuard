from invoiceguard.ratelimit import RateLimiter


def test_sliding_window_and_independent_keys():
    now = [0.0]
    rl = RateLimiter(2, 60.0, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    now[0] = 59.9
    assert not rl.allow("a")
    now[0] = 60.1
    assert rl.allow("a")


def test_count_and_record_track_failures_without_consuming():
    now = [0.0]
    rl = RateLimiter(3, 60.0, clock=lambda: now[0])
    assert rl.count("ip") == 0 and rl.count("ip") == 0
    rl.record("ip"); rl.record("ip")
    assert rl.count("ip") == 2
    now[0] = 61
    assert rl.count("ip") == 0
