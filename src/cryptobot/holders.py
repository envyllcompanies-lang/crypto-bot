"""V1's lookup checks: the top-5 holders and the token's X link (M6).

V1 avoids tokens whose five largest holders look like one bundle (SOL balances that are
alike, funding that is clustered) and trades only tokens whose X link is a community.
His example of a bundle: holders with 0.02, 0.1, 0.03 and 0.1 SOL, "almost all funded
25 minutes ago".

These need network calls, so they run once per token, when it first passes every other
rule, and the answer is saved with the run's events so a replay sees exactly what the
live run saw. Calls: read-only JSON-RPC (getMultipleAccounts, getSignaturesForAddress,
getTransaction; at most 11 per token, rate-limited) and one GET of the token's metadata
file, the `uri` its creator set at launch, which lists its X, Telegram and website links.

Definitions (our proposals; V1 gives examples, not thresholds):
- Balances alike: some ALIKE_COUNT of the five hold within a factor of ALIKE_RATIO of
  each other, all under ALIKE_MAX_LAMPORTS (bundle wallets are small and fresh; a live
  test showed ordinary traders with 6-10 SOL each would otherwise count as alike).
- Funding clustered: at least CLUSTER_MIN of the five share a funder (the account that
  paid for or sent SOL in the wallet's first transaction), or at least CLUSTER_MIN made
  their first transaction within FRESH_MS before the lookup.
- X link: x.com/i/communities/... is a community; .../status/... a tweet; anything else
  on x.com or twitter.com a profile. No X link fails the check.

Metadata is text written by token creators: it is parsed for links only, never acted on.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from urllib.parse import urlparse

import polars as pl

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
TOP_N = 5
ALIKE_COUNT = 4
ALIKE_RATIO = 5.0
ALIKE_MAX_LAMPORTS = 1_000_000_000  # 1 SOL
CLUSTER_MIN = 3
FRESH_MS = 60 * 60 * 1000
SIGNATURE_PAGE = 1000  # a wallet with this many signatures or more is not fresh
MIN_INTERVAL_S = 0.25  # at most 4 requests a second, well inside the public RPC's limits
MAX_QUEUE = 20  # beyond this, a lookup is skipped and recorded as "busy"
METADATA_MAX_BYTES = 65_536
X_HOSTS = ("x.com", "twitter.com")

LOOKUP_SCHEMA = {
    "seq": pl.UInt64,
    "received_ms": pl.Int64,
    "event_index": pl.UInt16,
    "mint": pl.Utf8,
    "requested_ms": pl.Int64,
    "result": pl.Utf8,  # JSON: wallets, balances, first_ms, funders, x_link, x_kind, ...
    "error": pl.Utf8,
}

Call = Callable[[str, list], object]
Fetch = Callable[[str], dict | None]


def rpc(method: str, params: list, url: str = PUBLIC_RPC, retries: int = 4) -> object:
    """One JSON-RPC call with exponential backoff on network errors and rate limits."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(retries):
        request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                out = json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
            continue
        if "error" in out:
            if attempt == retries - 1:
                raise RuntimeError(f"{method}: {out['error']}")
            time.sleep(2**attempt)
            continue
        return out["result"]
    raise RuntimeError(f"{method}: no result")


def throttled(url: str = PUBLIC_RPC) -> Call:
    last = [0.0]

    def call(method: str, params: list) -> object:
        wait = last[0] + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()
        return rpc(method, params, url)

    return call


def fetch_metadata(uri: str) -> dict | None:
    """The token's metadata JSON (https only, at most 64 KB)."""
    if not uri or not uri.startswith("https://"):
        return None
    request = urllib.request.Request(uri, headers={"User-Agent": "crypto-bot/0.1"})
    with urllib.request.urlopen(request, timeout=10) as resp:
        raw = resp.read(METADATA_MAX_BYTES + 1)
    if len(raw) > METADATA_MAX_BYTES:
        return None
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


def x_link_kind(url: object) -> str | None:
    """ "community", "tweet" or "profile" for an X link; None if it isn't one."""
    if not isinstance(url, str):
        return None
    parsed = urlparse(url.strip() if "://" in url else "https://" + url.strip())
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in X_HOSTS):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if parts[:2] == ["i", "communities"]:
        return "community"
    if len(parts) >= 3 and parts[1] == "status":
        return "tweet"
    return "profile" if parts else None


def funder_of(tx: dict | None, wallet: str) -> str | None:
    """Who paid for or sent the SOL in a wallet's first transaction."""
    if not tx:
        return None
    keys = tx["transaction"]["message"]["accountKeys"]
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in keys]
    if keys and keys[0] != wallet:
        return keys[0]
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances", []), meta.get("postBalances", [])
    drops = [(a - b, k) for k, a, b in zip(keys, pre, post, strict=False) if k != wallet]
    if not drops:
        return None
    drop, key = max(drops)
    return key if drop > 0 else None


def _short(value: object) -> str | None:
    return value[:200] if isinstance(value, str) else None


def lookup(wallets: list[str], uri: str, call: Call, fetch: Fetch = fetch_metadata) -> dict:
    """Balances, first-transaction times and funders of the wallets, and the X link."""
    balances: list[int] = []
    first_ms: list[int | None] = []
    funders: list[str | None] = []
    if wallets:
        accounts = call("getMultipleAccounts", [wallets, {"encoding": "base64"}])["value"]
        balances = [a["lamports"] if a else 0 for a in accounts]
    for wallet in wallets:
        sigs = call("getSignaturesForAddress", [wallet, {"limit": SIGNATURE_PAGE}])
        if not sigs or len(sigs) >= SIGNATURE_PAGE:
            first_ms.append(None)
            funders.append(None)
            continue
        oldest = sigs[-1]
        first_ms.append(oldest["blockTime"] * 1000 if oldest.get("blockTime") else None)
        tx = call(
            "getTransaction",
            [oldest["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        )
        funders.append(funder_of(tx, wallet))
    out = {"wallets": wallets, "balances": balances, "first_ms": first_ms, "funders": funders}
    try:
        meta = fetch(uri) or {}
        x_link = meta.get("twitter") or meta.get("x")
        out |= {
            "x_link": _short(x_link),
            "x_kind": x_link_kind(x_link) or "none",
            "telegram": _short(meta.get("telegram")),
            "website": _short(meta.get("website")),
        }
    except Exception as exc:  # metadata unreachable: the X check is then not evaluated
        out |= {"x_kind": None, "metadata_error": type(exc).__name__}
    return out


def balances_alike(balances: list[int]) -> bool:
    if len(balances) < ALIKE_COUNT:
        return False
    s = sorted(balances)
    for i in range(len(s) - ALIKE_COUNT + 1):
        hi = s[i + ALIKE_COUNT - 1]
        if hi <= ALIKE_MAX_LAMPORTS and hi <= ALIKE_RATIO * max(s[i], 1):
            return True
    return False


def funding_clustered(funders: list[str | None], first_ms: list[int | None], now_ms: int) -> bool:
    counts = Counter(f for f in funders if f)
    shared = max(counts.values(), default=0) >= CLUSTER_MIN
    fresh = sum(1 for t in first_ms if t is not None and now_ms - t <= FRESH_MS)
    return shared or fresh >= CLUSTER_MIN


def classify(result: dict, now_ms: int) -> dict[str, bool | None]:
    """V1's lookup checks: True passes, False fails, None not evaluated."""
    x_kind = result.get("x_kind")
    return {
        "x_link_is_community": None if x_kind is None else x_kind == "community",
        "top5_sol_balances_differ": not balances_alike(result["balances"]),
        "top5_funding_not_clustered": not funding_clustered(
            result["funders"], result["first_ms"], now_ms
        ),
    }


class LookupWorker:
    """Runs lookups one at a time beside the live stream and hands each result back."""

    def __init__(
        self,
        on_result: Callable[[dict], None],
        add_row: Callable[[dict], None],
        next_seq: Callable[[], int],
        now_ms: Callable[[], int],
        url: str = PUBLIC_RPC,
    ) -> None:
        self.queue: asyncio.Queue[tuple[str, list[str], str, int]] = asyncio.Queue()
        self.on_result, self.add_row = on_result, add_row
        self.next_seq, self.now_ms = next_seq, now_ms
        self.call = throttled(url)

    def request(self, mint: str, wallets: list[str], uri: str, ts: int) -> None:
        if self.queue.qsize() >= MAX_QUEUE:
            self._deliver(mint, ts, None, "busy")
        else:
            self.queue.put_nowait((mint, wallets, uri, ts))

    async def run(self) -> None:
        while True:
            mint, wallets, uri, ts = await self.queue.get()
            result, error = None, None
            try:
                result = await asyncio.to_thread(lookup, wallets, uri, self.call)
            except Exception as exc:  # recorded; the holder checks are then not evaluated
                error = type(exc).__name__
            self._deliver(mint, ts, result, error)

    def _deliver(self, mint: str, ts: int, result: dict | None, error: str | None) -> None:
        row = {
            "seq": self.next_seq(),
            "received_ms": self.now_ms(),
            "event_index": 0,
            "mint": mint,
            "requested_ms": ts,
            "result": None if result is None else json.dumps(result),
            "error": error,
        }
        self.add_row(row)
        self.on_result(row)
