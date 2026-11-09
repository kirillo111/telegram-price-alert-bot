import asyncio

import pytest

from bot import AlertStore, fmt_num, is_triggered, parse_alert, run_checks


def test_fmt_num():
    assert fmt_num(1000000.0) == "1,000,000"
    assert fmt_num(75506.5) == "75,506.5"
    assert fmt_num(2.5) == "2.5"
    assert fmt_num(0.00001234) == "0.00001234"
    assert "e" not in fmt_num(1e-8) and "e" not in fmt_num(1e12)


def test_parse_ok():
    assert parse_alert("BTC > 100000") == ("BTC/USDT", ">", 100000.0)
    assert parse_alert("eth/usdc <= 2 500".replace(" 500", ",5")) == ("ETH/USDC", "<", 2.5)
    assert parse_alert("sol>150.5") == ("SOL/USDT", ">", 150.5)


@pytest.mark.parametrize("bad", ["", "BTC", "BTC > abc", "BTC > 0", "> 5", "BTC = 5"])
def test_parse_bad(bad):
    assert parse_alert(bad) is None


def test_triggered():
    assert is_triggered(">", 100, 100) and is_triggered(">", 100, 101)
    assert not is_triggered(">", 100, 99.9)
    assert is_triggered("<", 100, 100) and not is_triggered("<", 100, 100.1)


def test_store_roundtrip(tmp_path):
    p = tmp_path / "a.json"
    s = AlertStore(p)
    a = s.add(1, "BTC/USDT", ">", 100.0)
    s.add(2, "ETH/USDT", "<", 5.0)
    s2 = AlertStore(p)
    assert len(s2.alerts) == 2 and s2.next_id == 3
    assert not s2.remove(2, a["id"])          # чужой алерт удалить нельзя
    assert s2.remove(1, a["id"])
    assert len(AlertStore(p).alerts) == 1


def test_store_broken_file(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{not json")
    assert AlertStore(p).alerts == []


class FakeExchange:
    def __init__(self, prices):
        self.prices, self.calls = prices, []

    async def fetch_ticker(self, symbol):
        self.calls.append(symbol)
        if symbol not in self.prices:
            raise RuntimeError("no such pair")
        return {"last": self.prices[symbol]}


def test_run_checks(tmp_path):
    s = AlertStore(tmp_path / "a.json")
    s.add(1, "BTC/USDT", ">", 100.0)   # сработает
    s.add(1, "BTC/USDT", "<", 50.0)    # нет
    s.add(2, "ETH/USDT", "<", 10.0)    # сработает у другого чата
    s.add(3, "XXX/USDT", ">", 1.0)     # пара недоступна: пропускаем
    sent = []

    async def send(chat, text):
        sent.append((chat, text))

    ex = FakeExchange({"BTC/USDT": 101.0, "ETH/USDT": 9.0})
    fired = asyncio.run(run_checks(s, ex, send))
    assert fired == 2
    assert ex.calls.count("BTC/USDT") == 1          # цена берётся один раз
    assert {c for c, _ in sent} == {1, 2}
    left = {(a["chat_id"], a["symbol"], a["op"]) for a in s.alerts}
    assert left == {(1, "BTC/USDT", "<"), (3, "XXX/USDT", ">")}


def test_send_failure_keeps_alert(tmp_path):
    s = AlertStore(tmp_path / "a.json")
    s.add(1, "BTC/USDT", ">", 1.0)

    async def send(chat, text):
        raise RuntimeError("telegram down")

    fired = asyncio.run(run_checks(s, FakeExchange({"BTC/USDT": 5.0}), send))
    assert fired == 0 and len(s.alerts) == 1
