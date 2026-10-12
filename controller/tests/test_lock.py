from contour.lock import MemoryBackend, acquire


def test_second_acquire_on_same_path_returns_none():
    backend = MemoryBackend()
    path = "/srv/ai/sync.lock"

    first = acquire(path, backend=backend)
    assert first is not None

    second = acquire(path, backend=backend)
    assert second is None


def test_release_allows_subsequent_acquire():
    backend = MemoryBackend()
    path = "/srv/ai/sync.lock"

    with acquire(path, backend=backend) as lock:
        assert lock is not None
        assert acquire(path, backend=backend) is None

    third = acquire(path, backend=backend)
    assert third is not None
