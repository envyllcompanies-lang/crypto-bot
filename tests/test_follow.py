"""Copy trading: follow the first leader, or wait for a second one."""

import numpy as np

from cryptobot.follow import follow_token
from tests.test_screen import token


def test_follows_first_leader_and_waits_for_a_second():
    _, ts, vsol, vtok = token(5)
    n = ts.size
    users = np.array([f"w{k % 7}" for k in range(n)], dtype=object)
    is_buy = np.diff(np.concatenate(([vsol[0] - 1], vsol))) >= 0
    leaders = {"t": frozenset({"w3", "w5"})}
    rows = follow_token(ts, vsol, vtok, users, is_buy, int(ts[0]) - 1, leaders)
    names = {r[0] for r in rows}
    assert names == {"copy_first_t", "copy_two_t"}
    first = next(r for r in rows if r[0] == "copy_first_t")
    want = int(np.flatnonzero(np.isin(users, ["w3", "w5"]) & is_buy)[0])
    assert first[4] == int(ts[want])  # decided on the leader's own buy
    leader_exit = [r for r in rows if r[0] == "copy_first_t" and r[1] == "leader"]
    assert leader_exit and leader_exit[0][7] in {"leader", "timeout", "graduated"}


def test_no_leader_no_trade():
    _, ts, vsol, vtok = token(6)
    users = np.array(["x"] * ts.size, dtype=object)
    is_buy = np.ones(ts.size, bool)
    assert follow_token(ts, vsol, vtok, users, is_buy, 0, {"t": frozenset({"w"})}) == []
