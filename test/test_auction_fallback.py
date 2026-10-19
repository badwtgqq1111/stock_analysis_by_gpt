from datetime import datetime

from backend.services import auction_service as auc
from backend.services import live_quotes as lq


def test_auction_falls_back_to_tencent_snapshot_when_eastmoney_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(auc, "ARCHIVE", tmp_path)
    monkeypatch.setattr(auc, "_eastmoney_trends", lambda code: (None, "ProxyError"))
    monkeypatch.setattr(auc, "_tencent_snapshot", lambda code: {
        "time": "2026-10-19 09:20:00", "clock": "09:20", "price": 12.3,
        "avg": None, "open": 12.2, "high": 12.4, "low": 12.1,
        "volume_shares": 0, "amount_yuan": 0.0, "kind": "snapshot",
        "prev_close": 12.0,
    })
    monkeypatch.setattr(lq, "now_cn", lambda: datetime(2026, 10, 19, 9, 20, tzinfo=lq.CN_TZ))
    monkeypatch.setattr(auc, "_EM_DOWN_UNTIL", 0.0)
    lq.cache_clear()

    body = auc.auction("605499.SH", "2026-10-19")
    assert body["available"] is True
    assert body["source"] == "tencent_preopen_snapshot"
    assert body["degraded"] is True
    assert body["points"][0]["price"] == 12.3
    assert body["points"][0]["clock"] == "09:20"
    # 第二次走归档，不重复请求东财；快照按分钟追加。
    lq.cache_clear()
    body2 = auc.auction("605499.SH", "2026-10-19")
    assert body2["available"] is True
    assert len(body2["points"]) == 1
