"""Phase 7.10: dice_browser.resume_delivery -- fully offline, the
Supabase Storage call is monkeypatched. Never hits real storage.
"""
from __future__ import annotations

import dice_browser.resume_delivery as resume_delivery


def test_returns_true_immediately_when_a_real_file_already_exists(tmp_path, monkeypatch):
    dest = tmp_path / "resume.pdf"
    dest.write_bytes(b"%PDF-1.4 already here")

    calls = []
    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: calls.append("called"))

    result = resume_delivery.ensure_resume_available("candidate-1", str(dest))

    assert result is True
    assert calls == []  # never touched Supabase -- the local file was already usable


def test_downloads_when_destination_is_missing(tmp_path, monkeypatch):
    dest = tmp_path / "nested" / "resume.pdf"
    downloaded_paths = []

    class _FakeBucket:
        def download(self, path):
            downloaded_paths.append(path)
            return b"%PDF-1.4 real bytes from storage"

    class _FakeStorage:
        def from_(self, bucket):
            assert bucket == "resumes"
            return _FakeBucket()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: _FakeClient())

    result = resume_delivery.ensure_resume_available("candidate-1", str(dest))

    assert result is True
    assert downloaded_paths == ["candidate-1/resume.pdf"]
    assert dest.read_bytes() == b"%PDF-1.4 real bytes from storage"


def test_downloads_when_destination_is_an_empty_file(tmp_path, monkeypatch):
    dest = tmp_path / "resume.pdf"
    dest.touch()  # the exact "touch" bug this module exists to fix

    class _FakeBucket:
        def download(self, path):
            return b"%PDF-1.4 real bytes"

    class _FakeStorage:
        def from_(self, bucket):
            return _FakeBucket()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: _FakeClient())

    result = resume_delivery.ensure_resume_available("candidate-1", str(dest))

    assert result is True
    assert dest.stat().st_size > 0


def test_returns_false_when_storage_is_unreachable(tmp_path, monkeypatch):
    dest = tmp_path / "resume.pdf"

    def _boom():
        raise ConnectionError("Supabase unreachable")

    monkeypatch.setattr("db.supabase_client.get_supabase_client", _boom)

    result = resume_delivery.ensure_resume_available("candidate-1", str(dest))

    assert result is False
    assert not dest.exists()


def test_returns_false_when_no_resume_stored_for_candidate(tmp_path, monkeypatch):
    dest = tmp_path / "resume.pdf"

    class _FakeBucket:
        def download(self, path):
            raise Exception("not found")

    class _FakeStorage:
        def from_(self, bucket):
            return _FakeBucket()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: _FakeClient())

    result = resume_delivery.ensure_resume_available("candidate-1", str(dest))

    assert result is False


# Phase 8A: resume_exists_in_storage -- readiness.py's cheap, no-download check.
def test_resume_exists_in_storage_true_when_real_file_present(monkeypatch):
    class _FakeBucket:
        def list(self, path):
            return [{"name": "resume.pdf", "metadata": {"size": 106538}}]

    class _FakeStorage:
        def from_(self, bucket):
            return _FakeBucket()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: _FakeClient())

    assert resume_delivery.resume_exists_in_storage("candidate-1") is True


def test_resume_exists_in_storage_false_when_zero_byte(monkeypatch):
    class _FakeBucket:
        def list(self, path):
            return [{"name": "resume.pdf", "metadata": {"size": 0}}]

    class _FakeStorage:
        def from_(self, bucket):
            return _FakeBucket()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: _FakeClient())

    assert resume_delivery.resume_exists_in_storage("candidate-1") is False


def test_resume_exists_in_storage_false_when_no_files(monkeypatch):
    class _FakeBucket:
        def list(self, path):
            return []

    class _FakeStorage:
        def from_(self, bucket):
            return _FakeBucket()

    class _FakeClient:
        storage = _FakeStorage()

    monkeypatch.setattr("db.supabase_client.get_supabase_client", lambda: _FakeClient())

    assert resume_delivery.resume_exists_in_storage("candidate-1") is False


def test_resume_exists_in_storage_false_when_storage_unreachable(monkeypatch):
    def _boom():
        raise ConnectionError("Supabase unreachable")

    monkeypatch.setattr("db.supabase_client.get_supabase_client", _boom)

    assert resume_delivery.resume_exists_in_storage("candidate-1") is False
