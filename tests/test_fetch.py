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


def test_an_oversized_partial_file_is_downloaded_again(raw, monkeypatch):
    import io

    part = raw / "o__n" / "a.parquet.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"abcabcabc")  # two downloads appended to it: 9 bytes, not 3
    asked = []

    def urlopen(req, timeout):
        asked.append(req.get_header("Range"))
        resp = io.BytesIO(b"abc")
        resp.status = 200
        return _Ctx(resp)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    dest = fetch.download("o/n", "a.parquet", 3)
    assert dest.read_bytes() == b"abc" and asked == [None]  # whole file, no Range


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self.resp

    def __exit__(self, *exc):
        return False


def test_a_second_fetch_of_the_same_dataset_refuses_to_start(raw):
    held = fetch.lock("o/n")
    with pytest.raises(SystemExit, match="already running"):
        fetch.lock("o/n")
    held.close()
    fetch.lock("o/n").close()  # free again once the first one ends
