import json
from decimal import Decimal as D

from core.index import IndexState
from core.market import parse_market
from core.orderbook import OrderBook


def test_orderbook_snapshot_and_delta_docs_shape():
    b = OrderBook()
    b.apply_snapshot({
        "market_ticker": "FED-23DEC-T3.00",
        "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
        "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
    })
    assert b.best_yes_bid == D("0.22") and b.best_yes_ask == D("0.44")
    b.apply_delta({"price_dollars": "0.2200", "delta_fp": "-333.00", "side": "yes"})
    assert b.best_yes_bid == D("0.08")
    lad = b.ladder()
    assert lad["asks"][0] == (D("0.44"), D("146.00"))


def test_index_parsing_and_quarter_close():
    ix = IndexState()
    close = 1_700_000_100_000 - (1_700_000_100_000 % 900_000) + 900_000
    msg = {
        "index_id": "BRTI", "received_at": close,
        "data": json.dumps({"type": "value", "id": "BRTI", "time": close, "value": "68000.12"}),
        "avg_60s_data": {"value": "67990.50000000", "window_size": 59},
        "last_60s_windowed_average_15min": {"value": "67995.00000000", "window_size": 60,
                                             "window_end_ts_exclusive": close + 1},
    }
    ix.apply(msg)
    assert ix.value == D("68000.12") and ix.avg60 == D("67990.5")
    assert ix.final_n == 60 and ix.settlement_for(close) == (D("67995.00000000"), 60)
    ix.apply({**msg, "last_60s_windowed_average_15min": None})
    assert ix.final_avg is None and ix.settlement_for(close) is not None


def test_parse_market():
    m = parse_market({"ticker": "KXBTC15M-26OCT201215-15", "open_time": "2026-10-20T16:00:00Z",
                      "close_time": "2026-10-20T16:15:00Z", "floor_strike": 82513.98})
    assert m.target == D("82513.98") and m.close_ms - m.open_ms == 900_000


def test_signer_roundtrip():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    import base64
    from core.auth import KalshiSigner

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    h = KalshiSigner("kid", pem).sign("get", "/trade-api/ws/v2?x=1", ts_ms=123)
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "123"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"123GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
