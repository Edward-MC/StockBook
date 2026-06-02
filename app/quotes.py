"""Live A-share quote fetching by security code.

Sources (failover, in order): Tencent (qt.gtimg.cn), Sina (hq.sinajs.cn),
Eastmoney (push2.eastmoney.com). The first source that returns data wins; if a
source errors at the transport level we fall through to the next. All sources
return both the name and the latest price. Networking and parsing are split so
parsers are unit-testable without hitting the network.

The `source` field on PriceQuote is set to "auto" for prices fetched here.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, Iterable, List, Optional, Protocol, Tuple

import httpx

from . import config

# A-share trading sessions (local/CST): morning 09:30–11:30, afternoon 13:00–15:00.
_AM = (dt.time(9, 30), dt.time(11, 30))
_PM = (dt.time(13, 0), dt.time(15, 0))


def is_trading_session(when: Optional[dt.datetime]) -> bool:
    """True if `when` falls inside an A-share trading session (Mon–Fri only).

    Holidays are not detected (no trading calendar in v1), so a weekday holiday
    during market hours is treated as 'live' — acceptable for a personal tool.
    """
    if when is None or when.weekday() >= 5:  # Sat/Sun
        return False
    t = when.time()
    return _AM[0] <= t <= _AM[1] or _PM[0] <= t <= _PM[1]

_TENCENT_URL = "https://qt.gtimg.cn/q="
_SINA_URL = "https://hq.sinajs.cn/list="
_EASTMONEY_URL = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
                  "?fields=f1,f2,f12,f13,f14&secids=")
_TIMEOUT = 6.0
# Last source that successfully served a fetch (diagnostic; surfaced in refresh).
LAST_SOURCE: Optional[str] = None


def to_qq_symbol(code: str, market: str = "CN") -> Optional[str]:
    """Map an A-share code to a Tencent symbol like 'sh510300' / 'sz159915'.

    `market` may be an explicit exchange ("SH"/"SZ") to override the heuristic
    (escape hatch for codes the digit rule gets wrong, e.g. via PUT on a
    security). For "CN" we classify by code prefix:
      - 5/6/9 → Shanghai; 0/2/3 → Shenzhen
      - 1xxxxx: Shenzhen funds (15/16/18) etc., except Shanghai convertible
        bonds 110/111/113/118 → Shanghai
    Returns None for codes we can't classify (e.g. Beijing exchange 4/8/920) —
    callers should surface these rather than fail silently.
    """
    c = (code or "").strip()
    if not c.isdigit():
        return None
    if market == "SH":
        return "sh" + c
    if market == "SZ":
        return "sz" + c
    if market != "CN":
        return None
    if c.startswith("920"):
        return None  # Beijing Stock Exchange (920 segment) — not Shanghai
    if c[0] in "569":
        return "sh" + c
    if c[0] in "0123":
        if c[:3] in ("110", "111", "113", "118"):  # Shanghai convertible bonds
            return "sh" + c
        return "sz" + c
    return None


def parse_tencent(text: str) -> Dict[str, dict]:
    """Parse a Tencent quote response into {code: {"price": float, "name": str}}.

    Each record looks like: v_sh510300="1~沪深300ETF~510300~3.991~...";
    Field [1]=name, [2]=code, [3]=current price.
    """
    out: Dict[str, dict] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or '="' not in line:
            continue
        try:
            body = line.split('="', 1)[1].rstrip('"')
            f = body.split("~")
            code, name, price = f[2], f[1], float(f[3])
        except (IndexError, ValueError):
            continue
        if price > 0:
            out[code] = {"price": price, "name": name}
    return out


def to_em_secid(code: str, market: str = "CN") -> Optional[str]:
    """Eastmoney secid: '1.<code>' for Shanghai, '0.<code>' for Shenzhen."""
    sym = to_qq_symbol(code, market)
    if not sym:
        return None
    return f"{'1' if sym.startswith('sh') else '0'}.{code}"


def _qq_symbols(items) -> Dict[str, str]:
    """{tencent/sina symbol -> code} for the resolvable items."""
    out: Dict[str, str] = {}
    for code, market in items:
        sym = to_qq_symbol(code, market)
        if sym:
            out[sym] = code
    return out


# --------------------------------------------------------------------------- #
# Parsers (network-free, unit-testable)
# --------------------------------------------------------------------------- #
def parse_sina(text: str) -> Dict[str, dict]:
    """Sina: var hq_str_sh510300="名称,今开,昨收,现价,...";  (price at index 3)."""
    out: Dict[str, dict] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("var hq_str_") or "=" not in line:
            continue
        try:
            head, body = line.split("=", 1)
            sym = head.replace("var hq_str_", "").strip()
            f = body.strip().strip('"').split(",")
            code, name, price = sym[2:], f[0], float(f[3])
        except (IndexError, ValueError):
            continue
        if price > 0:
            out[code] = {"price": price, "name": name}
    return out


def parse_eastmoney(text: str) -> Dict[str, dict]:
    """Eastmoney JSON: data.diff[].{f12 code, f14 name, f2 price×10^f1}."""
    out: Dict[str, dict] = {}
    try:
        diff = (json.loads(text).get("data") or {}).get("diff") or []
    except (ValueError, AttributeError):
        return out
    for d in diff:
        try:
            code, name = str(d["f12"]), d.get("f14")
            f2 = d.get("f2")
            if f2 in (None, "-"):
                continue
            dec = int(d.get("f1") or 2)
            price = float(f2) / (10 ** dec)
        except (KeyError, ValueError, TypeError):
            continue
        if price > 0:
            out[code] = {"price": price, "name": name}
    return out


# --------------------------------------------------------------------------- #
# Per-source fetchers — each raises httpx.HTTPError on transport failure.
# --------------------------------------------------------------------------- #
def _get(url: str, headers: Optional[dict] = None) -> httpx.Response:
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.get(url, headers=headers or {})
        r.raise_for_status()
        return r


# --------------------------------------------------------------------------- #
# Quote sources — each is a QuoteSource: a named backend that maps (code,
# market) pairs to {code: {"price","name"}}, raising httpx.HTTPError on
# transport failure. parse_* stay module-level pure helpers (unit-tested).
# --------------------------------------------------------------------------- #
class QuoteSource(Protocol):
    name: str
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]: ...


class TencentSource:
    name = "tencent"
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]:
        syms = _qq_symbols(items)
        if not syms:
            return {}
        r = _get(_TENCENT_URL + ",".join(syms), {"Referer": "https://finance.qq.com"})
        return parse_tencent(r.content.decode("gbk", errors="ignore"))


class SinaSource:
    name = "sina"
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]:
        syms = _qq_symbols(items)
        if not syms:
            return {}
        r = _get(_SINA_URL + ",".join(syms), {"Referer": "https://finance.sina.com.cn"})
        return parse_sina(r.content.decode("gbk", errors="ignore"))


class EastmoneySource:
    name = "eastmoney"
    def fetch(self, items: List[Tuple[str, str]]) -> Dict[str, dict]:
        secids = [s for s in (to_em_secid(c, m) for c, m in items) if s]
        if not secids:
            return {}
        r = _get(_EASTMONEY_URL + ",".join(secids))
        return parse_eastmoney(r.text)


# Registry: name -> source. Replaces the old _FETCHERS function map.
QUOTE_SOURCES: Dict[str, QuoteSource] = {
    s.name: s for s in (TencentSource(), SinaSource(), EastmoneySource())
}


def fetch_quotes(items: Iterable[Tuple[str, str]],
                 sources: Optional[List[str]] = None) -> Dict[str, dict]:
    """Fetch live quotes for (code, market) pairs, trying sources in order.
    Returns {code: {"price","name"}} from the first source that responds with
    data. Raises httpx.HTTPError only if EVERY tried source failed at transport
    level. Sets module-level LAST_SOURCE to the winning source name."""
    global LAST_SOURCE
    items = list(items)
    chain = sources if sources is not None else list(config.QUOTE_SOURCES)
    last_err: Optional[httpx.HTTPError] = None
    for name in chain:
        source = QUOTE_SOURCES.get(name)
        if source is None:
            continue
        try:
            out = source.fetch(items)
        except httpx.HTTPError as e:
            last_err = e
            continue
        if out:
            LAST_SOURCE = name
            return out
    if last_err is not None:
        raise last_err
    LAST_SOURCE = None
    return {}
