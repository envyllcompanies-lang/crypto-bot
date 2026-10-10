import pytest

from cryptobot import fetch


@pytest.fixture
def raw(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "RAW", tmp_path)
    return tmp_path


def test_ensure_downloads_only_missing_files(raw, monkeypatch):
    listing = [{"path": "a.parquet", "size": 3}, {"path": "b.parquet", "size": 2}]
    monkeypatch.setattr(fetch, "list_files", lambda ds, p: listing)
    got = []
    monkeypatch.setattr(fetch, "download", lambda ds, path, size: got.append(path))
    assert fetch.ensure("o/n") == raw / "o__n"
    assert got == ["a.parquet", "b.parquet"]


def test_ensure_uses_local_files_when_offline(raw, monkeypatch):
    def offline(ds, p):
        raise OSError("no network")

    monkeypatch.setattr(fetch, "list_files", offline)
    (raw / "o__n" / "sub").mkdir(parents=True)
    assert fetch.ensure("o/n", ("sub",)) == raw / "o__n"


def test_ensure_fails_offline_without_local_files(raw, monkeypatch):
    def offline(ds, p):
        raise OSError("no network")

    monkeypatch.setattr(fetch, "list_files", offline)
    with pytest.raises(OSError):
        fetch.ensure("o/n", ("sub",))


def test_download_skips_file_of_right_size(raw):
    dest = raw / "o__n" / "a.parquet"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"abc")
    assert fetch.download("o/n", "a.parquet", 3) == dest
