"""P2.73 公开来源个股调研管线（Bynewlt 未收录票的兜底）测试。

守住四件事：
  1. 纯函数口径：代码归一化 / 公告优先级 / 文本归一化（全角折叠、空白剥离）；
  2. 引文自动核对必须"逐字命中原文"：改写、编造、越界编号一律剔除，refs 连坐过滤；
  3. 台账原子写 + JSON 往返一致；
  4. 默认目标票只取 Bynewlt 台账中 not_found 的票（不把已覆盖的票重复劳动）。

不联网、不调用 DeepSeek、不下载任何文件。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cn_public_stock_research.py"


def _pipeline():
    """按路径加载管线脚本（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("cn_public_stock_research", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_pack() -> dict:
    return {
        "code": "002294.SZ", "name": "信立泰", "fetched_at": "2026-10-17T20:00:00+0800",
        "source": "public_web",
        "reports": [
            {"org": "东吴证券", "title": "2026年中报点评", "date": "2026-08-28",
             "rating": "买入", "eps_this_year": 0.92, "info_code": "AP1",
             "pdf": {"ok": True, "pages": 2, "pages_text": [
                 "信立泰 2026 年中报点评：业绩符合预期，CKM 管线持续推进。",
                 "投资要点：公司创新药管线进入兑现期，回购计划彰显信心，拟回购资金总金额不低于人民币30,000万元。"]}},
        ],
        "announcements": [
            {"title": "关于回购公司股份的进展公告", "date": "2026-10-09", "url": "https://static.cninfo.com.cn/x.PDF",
             "pdf_text": "截至本公告披露日，公司已通过集中竞价方式回购股份 500 万股，回购资金总金额不低于人民币30,000万元。"},
            {"title": "ESG 报告", "date": "2026-09-30", "url": "https://static.cninfo.com.cn/y.PDF", "pdf_text": None},
        ],
        "news": [
            {"title": "信立泰中标公告", "url": "https://example.com/news/1", "snippet": "公司中标某医院采购项目。",
             "text_ok": True, "text": "公司中标某医院采购项目，金额约1.2亿元。"},
        ],
        "quote": {"name": "信立泰", "price": 30.84, "pe_ttm": 51.05, "pb": 4.01, "mcap_yi": 343.73},
        "quant": {"stock_info": {"industry": "化学制药", "list_date": "20091127"}},
        "usable_items": 6,
    }


# ─── 1. 纯函数口径 ───────────────────────────────────────────────────────────
def test_norm_code_handles_common_spellings():
    module = _pipeline()
    assert module.norm_code("002294") == "002294.SZ"
    assert module.norm_code("600519") == "600519.SH"
    assert module.norm_code("430047") == "430047.BJ"
    assert module.norm_code("sz002294") == "002294.SZ"
    assert module.norm_code("600036.SH") == "600036.SH"
    assert module.norm_code("") == ""


def test_anno_priority_prefers_keyword_titles():
    module = _pipeline()
    items = [
        {"title": "关于会计政策变更的公告"},
        {"title": "关于回购公司股份的进展公告"},
        {"title": "第六届董事会决议公告"},
        {"title": "关于重大资产重组停牌的公告"},
    ]
    ordered = sorted(items, key=module.anno_priority)
    assert "回购" in ordered[0]["title"]
    assert "重大资产" in ordered[1]["title"]


def test_norm_for_match_folds_fullwidth_and_whitespace():
    module = _pipeline()
    assert module._norm_for_match("人民币 30,000万元（含）") == module._norm_for_match("人民币30,000万元(含)")
    assert module._norm_for_match("ＡB C") == "abc"


# ─── 2. 引文自动核对 ─────────────────────────────────────────────────────────
def test_verify_citations_checks_quotes_against_evidence():
    module = _pipeline()
    pack = _fake_pack()
    draft = {
        "citations": [
            {"id": "R1", "quote": "拟回购资金总金额不低于人民币30,000万元"},
            {"id": "A1", "quote": "已通过集中竞价方式回购股份 500 万股"},
            {"id": "R1", "quote": "这句话原文里根本不存在"},
            {"id": "N9", "quote": "越界编号"},
        ],
        "bull": [{"point": "回购彰显信心", "refs": ["R1", "N9"]}],
        "bear": [{"point": "估值不便宜", "refs": ["R1", "A1"]}],
        "risks": [{"point": "无", "refs": ["N9"]}],
    }
    out = module.verify_citations(pack, draft)
    verification = out["verification"]
    assert verification["checked"] == 4
    assert verification["verified"] == 2
    assert len(verification["failed"]) == 2
    assert [c["id"] for c in out["citations"]] == ["R1", "A1"]
    # 台账里第 2 页才提到回购金额 → 页号定位应为 2
    assert out["citations"][0].get("page") == 2
    assert out["citations"][0]["verified"] == "cite"
    # refs 连坐过滤：不存在 / 未核验的编号从要点里剔除，空要点整体丢弃
    assert out["bull"][0]["refs"] == ["R1"]
    assert out["bear"][0]["refs"] == ["R1", "A1"]
    assert out["risks"] == []


def test_verify_citations_allows_ellipsis_segments():
    module = _pipeline()
    pack = _fake_pack()
    draft = {
        "citations": [{"id": "A1", "quote": "公司已通过集中竞价方式…30,000万元"}],
        "bull": [], "bear": [], "risks": [],
    }
    out = module.verify_citations(pack, draft)
    assert out["verification"]["verified"] == 1


# ─── 3. 摘要与提示词 ─────────────────────────────────────────────────────────
def test_evidence_digest_counts():
    module = _pipeline()
    digest = module.evidence_digest(_fake_pack())
    assert digest["reports"] == 1 and digest["report_pdfs"] == 1
    assert digest["announcements"] == 2 and digest["announcement_pdfs"] == 1
    assert digest["news"] == 1 and digest["news_text"] == 1
    assert digest["brokers"] == ["东吴证券"]
    assert digest["source"] == "public_web"


def test_pack_to_prompt_includes_all_sections():
    module = _pipeline()
    prompt = module.pack_to_prompt(_fake_pack())
    for token in ["R1", "A1", "N1", "东吴证券", "回购资金总金额", "30.84", "化学制药"]:
        assert token in prompt


def test_tushare_facts_feed_prompt_and_quote_verification():
    module = _pipeline()
    pack = _fake_pack()
    pack["tushare"] = {
        "report_rc": [{"org": "东吴证券", "date": "20260828", "title": "2026年中报点评", "author": "李四",
                       "rating": "买入", "eps": 0.92, "pe": 25.0, "np_wan": 100000.0,
                       "max_price": None, "min_price": None, "forecast_period": "2026Q4"}],
        "repurchase": [{"ann_date": "20261009", "proc": "实施", "vol_wan": 500.0,
                        "amount_yi": 2.66, "high": 40.8, "low": 29.28}],
        "broker_recommend": [{"month": "202610", "brokers": ["东吴证券"]}],
        "errors": [],
    }
    facts = module.build_tushare_facts(pack)
    assert [fid for fid, _ in facts] == ["D1", "D2", "D3"]
    assert "东吴证券" in facts[0][1] and "2026-08-28" in facts[0][1]
    assert "2.66" in facts[1][1]
    prompt = module.pack_to_prompt(pack)
    assert "## Tushare 结构化数据" in prompt and "D1｜" in prompt and "D3｜" in prompt
    corpus = module.build_searchable_map(pack)
    assert "D1" in corpus and "D2" in corpus
    digest = module.evidence_digest(pack)
    assert digest["tushare"] == {"report_rc": 1, "repurchase": 1, "broker_recommend": 1}
    draft = {
        "citations": [
            {"id": "D2", "quote": "D2｜回购：2026-10-09 实施 已回购 500.0 万股/金额 2.66 亿元", "kind": "tushare", "source": "Tushare"},
            {"id": "D2", "quote": "已回购 500.0 万股/金额 2.66 亿元", "kind": "tushare", "source": "Tushare"},
            {"id": "D9", "quote": "越界编号"},
        ],
        "bull": [{"point": "回购推进", "refs": ["D2"]}, {"point": "假依据", "refs": ["D9"]}],
        "bear": [], "risks": [],
    }
    out = module.verify_citations(pack, draft)
    assert out["verification"]["verified"] == 2
    assert [c["id"] for c in out["citations"]] == ["D2", "D2"]
    assert len(out["bull"]) == 1 and out["bull"][0]["refs"] == ["D2"]


def test_verify_citations_tolerates_punctuation_variants():
    module = _pipeline()
    pack = _fake_pack()
    pack["quant"] = {"stock_info": {"industry": "化学制药", "list_date": "20091127"},
                     "lhb_90d": [{"date": "2026-10-12", "reason": "日振幅值达到15%的前5只证券",
                                  "net_buy_wan": -2097.5}]}
    draft = {
        "citations": [{"id": "Q", "quote": "价格 30.84，PE(TTM) 51.05，PB 4.01，总市值 343.73 亿元，"
                                          "行业 化学制药，上市日期 20091127；"
                                          "近 90 天龙虎榜：2026-10-12 日振幅值达到15%的前5只证券 净买-2097.5万"}],
        "bull": [], "bear": [], "risks": [],
    }
    out = module.verify_citations(pack, draft)
    assert out["verification"]["verified"] == 1


# ─── 4. 台账读写与目标票推导 ─────────────────────────────────────────────────
def test_state_roundtrip(tmp_path, monkeypatch):
    module = _pipeline()
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    state = module.load_state()
    assert state["stocks"] == {}
    state.setdefault("stocks", {})["002294.SZ"] = {"status": "fetched", "name": "信立泰"}
    module.save_state(state)
    again = module.load_state()
    assert again["stocks"]["002294.SZ"]["name"] == "信立泰"
    assert again["updated_at"]


def test_save_state_merged_preserves_other_codes(tmp_path, monkeypatch):
    module = _pipeline()
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    module.save_state({"updated_at": None, "stocks": {
        "600000.SH": {"status": "composed"},
        "002294.SZ": {"status": "fetched"},
    }})
    module.save_state_merged({"updated_at": None, "stocks": {"002294.SZ": {"status": "composed"}}},
                             "002294.SZ")
    again = module.load_state()
    assert again["stocks"]["600000.SH"]["status"] == "composed"
    assert again["stocks"]["002294.SZ"]["status"] == "composed"


def test_targets_from_bynewlt_filters_not_found(tmp_path, monkeypatch):
    module = _pipeline()
    fake_state = tmp_path / "bynewlt_state.json"
    fake_state.write_text(json.dumps({
        "stocks": {
            "002294.SZ": {"status": "not_found", "name": "信立泰"},
            "600028.SH": {"status": "fetched", "name": "中国石化"},
            "600036.SH": {"status": "not_found", "name": "招商银行"},
        }
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "BYNEWLT_STATE", fake_state)
    targets = module.targets_from_bynewlt()
    assert targets == {"002294.SZ": "信立泰", "600036.SH": "招商银行"}


def test_solve_pdf_challenge_returns_empty_without_script():
    module = _pipeline()
    assert module.solve_pdf_challenge("<html><body>hello</body></html>") == {}
