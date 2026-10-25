import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from twscrape import NoAccountError

from cryptobot import social, xscrape

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def user(uid, followers=10, age_days=400):
    return NS(id=uid, followersCount=followers, created=NOW - timedelta(days=age_days),
              statusesCount=50, blue=False, username=f"u{uid}")  # fmt: skip


def post(uid, minutes_ago, likes=1, views=100, **kw):
    return NS(user=user(uid, **kw), date=NOW - timedelta(minutes=minutes_ago),
              likeCount=likes, viewCount=views, retweetCount=0)  # fmt: skip


def test_contract_address_posts_count_authors_reach_and_fresh_accounts():
    posts = [
        post(1, 10, likes=5, views=1000, followers=5000),
        post(1, 30, followers=5000),  # same author twice
        post(2, 90, age_days=3),  # older than an hour, fresh account
        post(3, 5, views=None, age_days=10),  # X sometimes has no view count
    ]
    m = xscrape.post_metrics(posts, NOW)
    assert m == {
        "x_ca_posts": 4.0, "x_ca_posts_1h": 3.0, "x_ca_authors": 3.0, "x_ca_likes": 8.0,
        "x_ca_views": 1200.0, "x_ca_max_followers": 5000.0, "x_ca_new_share": 2 / 3,
    }  # fmt: skip
    assert xscrape.post_metrics([], NOW)["x_ca_new_share"] == 0.0
    assert xscrape.ticker_metrics(posts, NOW) == {"x_tk_posts_1h": 3.0, "x_tk_authors": 3.0}


def test_ticker_and_link_parsing():
    assert xscrape.ticker_query("PEPE") == "$PEPE"
    assert xscrape.ticker_query("$wif2") == "$wif2"
    assert xscrape.ticker_query("GTA") is None  # 3 letters: stocks and other coins
    assert xscrape.ticker_query("🐸🐸🐸🐸") is None
    t = xscrape.link_target
    assert t("https://x.com/i/communities/1789") == ("community", "1789")
    assert t("twitter.com/abc/status/42?s=20") == ("tweet", "42")
    assert t("https://x.com/oracle7vn?s=11") == ("profile", "oracle7vn")
    assert t("https://x.com/home") is None
    assert t("https://x.com/search?q=pepe") is None
    assert t("https://notx.com/abc") is None
    assert t(None) is None


def test_link_lookups_become_numbers():
    c = NS(memberCount=120, moderatorCount=2)
    assert xscrape.community_metrics(c) == {"x_comm_members": 120.0, "x_comm_mods": 2.0}
    tw = NS(likeCount=7, retweetCount=3, viewCount=None, date=NOW - timedelta(hours=5),
            user=user(9, followers=40, age_days=2))  # fmt: skip
    m = xscrape.tweet_metrics(tw, NOW)
    assert m["x_post_likes"] == 7.0 and m["x_post_views"] == 0.0
    assert m["x_post_age_h"] == pytest.approx(5.0) and m["x_post_author_days"] == pytest.approx(2)
    p = xscrape.profile_metrics(user(9, followers=40, age_days=2), NOW)
    assert p == {"x_prof_followers": 40.0, "x_prof_posts": 50.0, "x_prof_blue": 0.0,
                 "x_prof_days": pytest.approx(2)}  # fmt: skip


def test_daily_cap_survives_restarts(tmp_path):
    cap = xscrape.DailyCap(tmp_path / "cap.json", per_day=2)
    assert cap.take() and cap.take() and not cap.take()
    assert xscrape.DailyCap(tmp_path / "cap.json", per_day=2).left() == 0


class FakePool:
    def __init__(self, active, error_msg=None):
        self.info = [{"username": "me", "active": active, "error_msg": error_msg, "total_req": 1}]

    async def accounts_info(self):
        return self.info


def scraper(tmp_path, pool, per_day=10):
    api = NS(pool=pool)
    return xscrape.XScraper(api=api, cap=xscrape.DailyCap(tmp_path / "c.json", per_day), gap_s=0)


async def no_account():
    raise NoAccountError("none")


def test_account_errors_tell_rate_limits_from_a_dead_account(tmp_path):
    x = scraper(tmp_path, FakePool(active=True))
    with pytest.raises(xscrape.RateLimited):
        x._call(no_account)
    x = scraper(tmp_path, FakePool(active=False, error_msg="(32) Could not authenticate you"))
    with pytest.raises(xscrape.AccountDead, match="Could not authenticate"):
        x._call(no_account)


def test_cap_stops_requests(tmp_path):
    x = scraper(tmp_path, FakePool(active=True), per_day=1)

    async def ok():
        return "got"

    assert x._call(ok) == "got"
    assert x._call(ok) is None  # the day's cap is used up: no request made


class FakeQueueClient:
    """Stands in for twscrape's QueueClient: answers each get() from a list."""

    answers: list = []
    calls: list = []

    def __init__(self, pool, queue, *a, **kw):
        assert queue == "SearchTimeline"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        FakeQueueClient.calls.append(json.loads(params["variables"])["rawQuery"])
        answer = FakeQueueClient.answers.pop(0)
        return None if answer is None else NS(json=lambda: answer)


EMPTY_TIMELINE = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
    "instructions": [{"type": "TimelineAddEntries", "entries": []}]}}}}}  # fmt: skip


def test_a_search_is_one_request_and_a_failure_is_never_zero_posts(tmp_path, monkeypatch):
    import twscrape.queue_client

    monkeypatch.setattr(twscrape.queue_client, "QueueClient", FakeQueueClient)
    FakeQueueClient.calls = []
    FakeQueueClient.answers = [EMPTY_TIMELINE, None, {"errors": [{"message": "Denied"}]}, "x"]
    x = scraper(tmp_path, FakePool(active=True))
    assert x.search('"Mint"') == []  # a real, empty answer: no posts
    assert FakeQueueClient.calls == ['"Mint"']  # one request, no second page
    with pytest.raises(xscrape.SearchFailed, match="blocked"):
        x.search('"Mint"')  # twscrape gave up (Cloudflare page, aborted request)
    with pytest.raises(xscrape.SearchFailed, match="Denied"):
        x.search('"Mint"')
    with pytest.raises(xscrape.SearchFailed):
        x.search('"Mint"')  # not a timeline at all


def test_coin_combines_searches_and_the_link(tmp_path, monkeypatch):
    x = scraper(tmp_path, FakePool(active=True))
    queries = []

    def search(q):
        queries.append(q)
        return [post(1, 5)]

    monkeypatch.setattr(x, "search", search)
    monkeypatch.setattr(x, "lookup", lambda target: NS(memberCount=80, moderatorCount=1))
    item = {"mint": "MintA", "symbol": "PEPE"}
    got = x.coin(item, "https://x.com/i/communities/1789", now=NOW)
    assert queries == ['"MintA"', "$PEPE"]
    assert got["x_ca_authors"] == 1.0 and got["x_tk_posts_1h"] == 1.0
    assert got["x_comm_members"] == 80.0
    monkeypatch.setattr(x, "search", lambda q: None)  # cap used up
    assert x.coin({"mint": "MintB", "symbol": "GTA"}, None) == {}


def test_results_reach_the_social_state(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text("")
    reader = social.Watchlist(path)
    xscrape.write_result({"mint": "MintA"}, {"x_ca_posts_1h": 3.0, "x_comm_members": 80.0}, path)
    with path.open("a") as f:
        f.write(json.dumps({"mint": "MintB", "values": {"evil": 1, "x_ca_posts": "9"}}) + "\n")
    items = reader.new()
    assert [social.x_values(i) for i in items] == [
        {"x_ca_posts_1h": 3.0, "x_comm_members": 80.0},
        {},  # unknown kinds and non-numbers are dropped
    ]
    rows = [(k, "MintA", v, "") for k, v in social.x_values(items[0]).items()]
    st = social.update({}, rows, 0)
    sig = social.signals(st, "MintA", None, None)
    assert sig["x_ca_posts_1h"] == 3.0 and sig["sources"] == 1  # one family: x


def test_login_saves_cookies_privately_and_never_prints_them(tmp_path, monkeypatch, capsys):
    db = tmp_path / "x_accounts.db"
    monkeypatch.setattr(xscrape, "ACCOUNTS_DB", db)
    monkeypatch.setattr(xscrape, "CAP_PATH", tmp_path / "cap.json")
    monkeypatch.setattr(xscrape, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "@my_account")
    secrets = iter(["a1b2c3secretauth", "d4e5f6secretct0"])
    monkeypatch.setattr(xscrape.getpass, "getpass", lambda prompt: next(secrets))
    monkeypatch.setattr(
        xscrape.XScraper, "lookup", lambda self, t: NS(username="my_account", followersCount=12)
    )
    xscrape.login()
    out = capsys.readouterr()
    assert "secretauth" not in out.out + out.err and "secretct0" not in out.out + out.err
    assert "Working: @my_account" in out.out
    assert oct(os.stat(db).st_mode & 0o777) == "0o600"
    from twscrape import AccountsPool

    accounts = asyncio.new_event_loop().run_until_complete(AccountsPool(str(db)).get_all())
    assert [a.username for a in accounts] == ["my_account"] and accounts[0].active
    assert xscrape.configured(db)


def test_login_refuses_values_that_are_not_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(xscrape, "ACCOUNTS_DB", tmp_path / "x.db")
    monkeypatch.setattr(xscrape, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "me")
    monkeypatch.setattr(xscrape.getpass, "getpass", lambda prompt: "auth_token=abc; ct0=def")
    with pytest.raises(SystemExit):
        xscrape.login()
    assert not (tmp_path / "x.db").exists()


def test_telemetry_is_off_and_no_browser_impersonation():
    assert os.environ["TWS_TELEMETRY"] == "0" and os.environ["DO_NOT_TRACK"] == "1"
    assert os.environ["TWS_HTTP_BACKEND"] == "httpx"


def test_login_needs_a_terminal_and_keeps_nothing_x_rejects(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    monkeypatch.setattr(xscrape, "ACCOUNTS_DB", db)
    monkeypatch.setattr(xscrape, "CAP_PATH", tmp_path / "cap.json")
    monkeypatch.setattr(xscrape, "interactive", lambda: False)
    with pytest.raises(SystemExit, match="interactive terminal"):
        xscrape.login()  # getpass would echo the cookies
    monkeypatch.setattr(xscrape, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "me_too")
    monkeypatch.setattr(xscrape.getpass, "getpass", lambda prompt: "abc123")

    def rejected(self, target):
        raise xscrape.AccountDead("(32) Could not authenticate you")

    monkeypatch.setattr(xscrape.XScraper, "lookup", rejected)
    with pytest.raises(SystemExit, match="nothing was kept"):
        xscrape.login()
    assert not db.exists()


class FakeWatch:
    def __init__(self, items):
        self.items = list(items)

    def new(self):
        out, self.items = self.items, []
        return out


class FakeScraper:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.cap = NS(per_day=300, left=lambda: 300)
        self.gap_s = 20
        self.is_active = True

    def coin(self, item, link):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def active(self):
        return self.is_active


@pytest.fixture
def service_env(tmp_path, monkeypatch):
    db = tmp_path / "x_accounts.db"
    db.write_bytes(b"saved")
    monkeypatch.setattr(xscrape, "ACCOUNTS_DB", db)
    monkeypatch.setattr(xscrape, "STATUS_PATH", tmp_path / "status.json")
    return tmp_path


def service(tmp_path, outcomes, coins=10):
    fake = FakeScraper(outcomes)
    items = [{"mint": f"M{i}", "uri": None} for i in range(coins)]
    svc = xscrape.Service(FakeWatch(items), make_scraper=lambda: fake,
                          link_of=lambda uri: None, results=tmp_path / "x.jsonl")  # fmt: skip
    return svc, fake


def state(tmp_path):
    return json.loads((tmp_path / "status.json").read_text())["state"]


def test_service_tells_rate_limits_from_an_expired_session(service_env):
    svc, _ = service(service_env, [xscrape.RateLimited()] * 3 + [{"x_ca_posts": 1.0}])
    for expect in ("rate-limited", "rate-limited", "probably logged out"):
        svc.step()
        svc.paused_until = 0  # skip the 15-minute pause
        assert state(service_env) == expect
    svc.step()  # a coin is read again
    assert state(service_env) == "reading" and svc.refusals == 0
    lines = (service_env / "x.jsonl").read_text().splitlines()
    assert [json.loads(line)["values"] for line in lines] == [{"x_ca_posts": 1.0}]


def test_service_pauses_when_searches_keep_failing_and_writes_no_zeros(service_env):
    fails = [xscrape.SearchFailed("blocked")] * xscrape.SEARCH_FAILS_BLOCKED
    svc, _ = service(service_env, fails)
    for _ in fails:
        svc.paused_until = 0
        svc.step()
    assert state(service_env) == "searches failing"
    assert svc.paused_until - time.time() > 1000  # a long pause, not a retry a minute
    assert not (service_env / "x.jsonl").exists()


def test_service_waits_for_a_new_login_after_x_stops_the_account(service_env):
    svc, fake = service(service_env, [xscrape.AccountDead("suspended"), {"x_ca_posts": 2.0}])
    svc.step()
    assert state(service_env) == "stopped by X"
    fake.is_active = False
    assert svc.step() == xscrape.DEAD_CHECK_S  # still stopped: reads nothing
    fake.is_active = True  # the owner ran `login` again
    svc.step()
    svc.step()
    assert (service_env / "x.jsonl").exists()


def test_service_stops_reading_after_logout(service_env):
    svc, _ = service(service_env, [{"x_ca_posts": 1.0}])
    xscrape.ACCOUNTS_DB.unlink()
    assert svc.step() == xscrape.DEAD_CHECK_S
    assert state(service_env) == "no login"
    assert not (service_env / "x.jsonl").exists()


def test_lookups_that_found_nothing_are_not_mentions():
    rows = [("x_ca_posts", "M", 0.0, ""), ("x_ca_posts_1h", "M", 0.0, ""),
            ("x_prof_followers", "M", 900.0, ""), ("tg_members", "M", 500.0, ""),
            ("dex_boosts_active", "M", 0.0, "")]  # fmt: skip
    sig = social.signals(social.update({}, rows, 0), "M", None, None)
    assert sig["x_ca_posts"] == 0.0 and sig["x_prof_followers"] == 900.0  # still logged
    assert sig["sources"] == 0 and sig["any"] is False
    rows.append(("x_ca_posts", "M", 3.0, ""))
    sig = social.signals(social.update({}, rows, 0), "M", None, None)
    assert sig["sources"] == 1  # now X mentions it
