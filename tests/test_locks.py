"""Tests for seekr_hatchery.locks."""

import threading
from pathlib import Path

from seekr_hatchery.locks import hatchery_lock


class TestHatcheryLock:
    def test_creates_lock_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with hatchery_lock("test.lock"):
            pass
        assert (tmp_path / ".hatchery" / "locks").is_dir()

    def test_creates_lock_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with hatchery_lock("test.lock"):
            pass
        assert (tmp_path / ".hatchery" / "locks" / "test.lock").exists()

    def test_can_reacquire_after_release(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with hatchery_lock("test.lock"):
            pass
        # Should not raise or block.
        with hatchery_lock("test.lock"):
            pass

    def test_exclusive_across_threads(self, tmp_path, monkeypatch):
        """Two threads must not hold the lock simultaneously."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        inside: list[int] = []
        errors: list[str] = []

        def _acquire() -> None:
            with hatchery_lock("test.lock"):
                inside.append(1)
                if len(inside) > 1:
                    errors.append("overlap detected")
                # Hold briefly to maximise overlap window.
                threading.Event().wait(timeout=0.05)
                inside.pop()

        t1 = threading.Thread(target=_acquire)
        t2 = threading.Thread(target=_acquire)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, errors

    def test_different_names_do_not_conflict(self, tmp_path, monkeypatch):
        """Different lock names must be independent."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        barrier = threading.Barrier(2)
        errors: list[str] = []

        def _hold_a() -> None:
            with hatchery_lock("a.lock"):
                barrier.wait(timeout=5)  # both inside simultaneously

        def _hold_b() -> None:
            with hatchery_lock("b.lock"):
                barrier.wait(timeout=5)

        t1 = threading.Thread(target=_hold_a)
        t2 = threading.Thread(target=_hold_b)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors
