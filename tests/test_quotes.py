"""Unit tests for live-quote symbol mapping and response parsing."""
import datetime as dt

import httpx
import pytest

from app import quotes
from app.quotes import (
    is_trading_session,
    parse_eastmoney,
    parse_sina,
    parse_tencent,
    to_em_secid,
    to_qq_symbol,
)


def test_is_trading_session():
    # Friday 2026-05-29
    assert is_trading_session(dt.datetime(2026, 5, 29, 10, 0)) is True   # morning
    assert is_trading_session(dt.datetime(2026, 5, 29, 14, 0)) is True   # afternoon
    assert is_trading_session(dt.datetime(2026, 5, 29, 12, 0)) is False  # lunch
    assert is_trading_session(dt.datetime(2026, 5, 29, 15, 30)) is False # after close
    assert is_trading_session(dt.datetime(2026, 5, 29, 9, 0)) is False   # pre-open
    # Saturday → closed all day
    assert is_trading_session(dt.datetime(2026, 5, 30, 10, 0)) is False
    assert is_trading_session(None) is False


def test_to_qq_symbol_market_prefix():
    assert to_qq_symbol("510300") == "sh510300"   # SH ETF
    assert to_qq_symbol("511260") == "sh511260"   # SH bond ETF
    assert to_qq_symbol("600519") == "sh600519"   # SH stock
    assert to_qq_symbol("159915") == "sz159915"   # SZ ETF
    assert to_qq_symbol("000001") == "sz000001"   # SZ stock
    assert to_qq_symbol("300750") == "sz300750"   # ChiNext


def test_to_qq_symbol_rejects():
    assert to_qq_symbol("AAPL", "US") is None
    assert to_qq_symbol("510300", "US") is None
    assert to_qq_symbol("abc") is None


def test_to_qq_symbol_shanghai_convertible_bond():
    assert to_qq_symbol("113050") == "sh113050"  # SH convertible bond, not sz
    assert to_qq_symbol("110059") == "sh110059"


def test_to_qq_symbol_explicit_exchange_override():
    assert to_qq_symbol("159915", "SH") == "sh159915"  # override wins
    assert to_qq_symbol("000001", "SZ") == "sz000001"


def test_to_qq_symbol_beijing_unresolved():
    assert to_qq_symbol("920819") is None  # Beijing exchange — surfaced, not guessed
    assert to_qq_symbol("830799") is None


def test_parse_tencent_extracts_price_and_name():
    text = ('v_sh510300="1~沪深300ETF~510300~3.991~3.985~3.990~12345";\n'
            'v_sz159915="51~创业板ETF~159915~2.030~2.010~2.020~999";')
    out = parse_tencent(text)
    assert out["510300"] == {"price": 3.991, "name": "沪深300ETF"}
    assert out["159915"]["price"] == 2.030


def test_parse_tencent_skips_garbage_and_nonpositive():
    text = 'noise;v_sh000000="1~停牌股~000000~0.00~0~";'
    assert parse_tencent(text) == {}


def test_parse_sina():
    text = ('var hq_str_sh510300="沪深300ETF,3.990,3.985,3.991,4.0,3.9";\n'
            'var hq_str_sz159915="创业板ETF,2.0,2.01,2.030,2.1,1.99";')
    out = parse_sina(text)
    assert out["510300"] == {"price": 3.991, "name": "沪深300ETF"}
    assert out["159915"]["price"] == 2.030


def test_parse_eastmoney():
    text = ('{"data":{"diff":['
            '{"f1":3,"f2":3991,"f12":"510300","f13":1,"f14":"沪深300ETF"},'
            '{"f1":3,"f2":2030,"f12":"159915","f13":0,"f14":"创业板ETF"}]}}')
    out = parse_eastmoney(text)
    assert out["510300"] == {"price": 3.991, "name": "沪深300ETF"}  # f2 / 10^f1
    assert out["159915"]["price"] == 2.030


def test_to_em_secid():
    assert to_em_secid("510300") == "1.510300"  # SH
    assert to_em_secid("159915") == "0.159915"  # SZ
    assert to_em_secid("920819") is None


class FakeQuoteSource:
    """A QuoteSource for tests — no network. Returns a canned result or raises."""
    def __init__(self, name, result=None, error=None):
        self.name = name
        self._result = result or {}
        self._error = error

    def fetch(self, items):
        if self._error is not None:
            raise self._error
        return self._result


def test_fetch_quotes_failover(monkeypatch):
    bad = FakeQuoteSource("tencent", error=httpx.ConnectError("down"))
    good = FakeQuoteSource("sina", result={"510300": {"price": 1.0, "name": "x"}})
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "tencent", bad)
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "sina", good)
    out = quotes.fetch_quotes([("510300", "CN")], sources=["tencent", "sina"])
    assert out["510300"]["price"] == 1.0
    assert quotes.LAST_SOURCE == "sina"


def test_fetch_quotes_all_sources_fail_raises(monkeypatch):
    bad = FakeQuoteSource("tencent", error=httpx.ConnectError("down"))
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "tencent", bad)
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "sina",
                        FakeQuoteSource("sina", error=httpx.ConnectError("down")))
    with pytest.raises(httpx.HTTPError):
        quotes.fetch_quotes([("510300", "CN")], sources=["tencent", "sina"])


def test_fetch_quotes_supports_new_registered_source(monkeypatch):
    # 扩展性:加一个新源 = 注册一个对象,fetch_quotes 无需改动即可用它。
    custom = FakeQuoteSource("custom", result={"510300": {"price": 9.9, "name": "c"}})
    monkeypatch.setitem(quotes.QUOTE_SOURCES, "custom", custom)
    out = quotes.fetch_quotes([("510300", "CN")], sources=["custom"])
    assert out["510300"]["price"] == 9.9
    assert quotes.LAST_SOURCE == "custom"
