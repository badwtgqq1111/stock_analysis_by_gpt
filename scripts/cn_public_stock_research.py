#!/usr/bin/env python3
"""P2.73 公开来源个股调研管线（Bynewlt 未收录票的兜底；searxng + 免费直连数据源）。

用户指示（2026-10-17）："如果用这个新的 skills 找不到对应股票信息，就使用
searxng / lightrag 实现类似个股研报分析能力。"

本脚本只负责**取证据**（合成稿由 agent 审核后写入 dossier/）。每票汇聚：

  1) 券商研报：东财研报列表（reportapi）→ 研报 PDF 全文（pdf.dfcfw.com，
     带 JS 反爬挑战，用 node 解 cookie 后下载；pypdf 抽取逐页文本）；
  2) 公司公告：巨潮 cninfo 列表 → 公告 PDF 全文（static.cninfo.com.cn）；
  3) 网络线索：本地 searxng（127.0.0.1:8888）聚合搜索 + 静态新闻页正文抓取；
  4) 行情与资金面：腾讯行情（PE/PB/市值）、东财资金流 120 日、龙虎榜、
     融资融券、解禁、股东户数、分红（datacenter / push2his）；
  5) Tushare 结构化数据（api.tushare.pro，需环境变量 TUSHARE_TOKEN）：券商预测
     report_rc（评级/EPS）、机构调研、回购、解禁、两融、股东户数、分红、筹码
     分布（cyq_perf）、券商金股（broker_recommend）、业绩预告/快报。

纪律（与 P2.72 一致）：
  * 决不编造——合成稿的每条引用都必须能在证据档中找到原文（`draft` 后自动逐条比对）；
  * 公开来源如实标注（source=public_web），不模拟 Bynewlt 的库内证据口径；
  * 免费源也要温和节流；单票失败不拖垮整批，错误逐票记账。

用法：
  python3 scripts/cn_public_stock_research.py fetch                 # 默认为 Bynewlt not_found 票
  python3 scripts/cn_public_stock_research.py fetch --codes 002294.SZ,601579.SH
  python3 scripts/cn_public_stock_research.py fetch --limit 3 --refetch
  python3 scripts/cn_public_stock_research.py status [--verbose]
  python3 scripts/cn_public_stock_research.py show --code 002294.SZ [--json]
  python3 scripts/cn_public_stock_research.py draft --code 002294.SZ      # DeepSeek 出草稿（含引文自动核对）
  python3 scripts/cn_public_stock_research.py draft --codes ... --sleep 2
  python3 scripts/cn_public_stock_research.py promote --code 002294.SZ    # 草稿→dossier（agent 复核后）
  python3 scripts/cn_public_stock_research.py mark --code ... --status ...

产物（output/results_cn/cn_public_research/）：
  state.json            状态台账（每票 status / 来源健康度 / 条数 / 时间）
  evidence/<code>.json  证据档（研报逐页文本/公告全文/新闻正文/行情资金面快照）
  dossier_draft/<code>.json  LLM 草稿 + 引文核对结果（verify 段）
  dossier/<code>.json   agent 复核后的最终稿（前端面板读取；source=public_web）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "output/results_cn/cn_public_research"
EVIDENCE_DIR = STORE / "evidence"
DRAFT_DIR = STORE / "dossier_draft"
DOSSIER_DIR = STORE / "dossier"
STATE_PATH = STORE / "state.json"
COOKIE_PATH = STORE / "pdf_cookies.json"
BYNEWLT_STATE = ROOT / "output/results_cn/bynewlt_research/state.json"

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

CST = timezone(timedelta(hours=8))

ST_QUEUED, ST_FETCHING, ST_FETCHED = "queued", "fetching", "fetched"
ST_NO_FUNDING = "no_data"          # 五个来源都没取到任何可用证据（罕见）
ST_ERROR = "error"
ST_COMPOSED = "composed"

REPORT_MONTHS = 18                 # 研报列表回看窗口
REPORT_MAX_LIST = 10               # 列表最多保留
REPORT_PDF_MAX = 4                 # 下载全文的研报数
REPORT_PAGE_CHARS = 2600           # 单页文本上限
ANNO_MAX_LIST = 30                 # 公告列表最多保留
ANNO_PDF_MAX = 5                   # 下载全文的公告数（按关键词优先）
NEWS_QUERIES = 5                   # searxng 查询数
NEWS_MAX_RESULTS = 8               # 每查询取回条数
NEWS_KEEP_MAX = 24                 # 相关性过滤后最多保留条数
NEWS_FETCH_MAX = 6                 # 抓正文的新闻页数
NEWS_TEXT_CHARS = 3000

ANNO_KEYWORDS = ["业绩预告", "业绩快报", "预增", "预减", "回购", "增持", "减持", "中标",
                 "合同", "诉讼", "仲裁", "立案", "处罚", "问询", "警示", "股权激励",
                 "重大资产", "收购", "重组", "定增", "发行", "解禁", "分红", "派息",
                 "退市", "停牌", "复牌", "审计", "会计差错"]

NEWS_DOMAIN_PRIORITY = ["finance.sina.com.cn", "vip.stock.finance.sina.com.cn",
                        "stock.finance.sina.com.cn", "finance.eastmoney.com",
                        "data.eastmoney.com", "www.163.com", "www.sohu.com", "xueqiu.com",
                        "www.thepaper.cn", "www.stcn.com", "www.cnstock.com", "www.yicai.com",
                        "www.cls.cn", "sgpjbg.com", "www.cfi.cn", "stock.cngold.org",
                        "www.nxny.com", "ddx.gubit.cn"]


def now_iso() -> str:
    return datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S%z")


def norm_code(raw: str) -> str:
    """`600519` / `sh600519` / `600519.SH` → `600519.SH`。"""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        number, _, market = text.partition(".")
        number, market = number.strip(), market.strip()
        if market in ("SH", "SZ", "BJ"):
            return f"{number}.{market}"
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            return f"{text[len(prefix):]}.{prefix}"
    if text.isdigit():
        market = "SH" if text[0] in "69" else ("BJ" if text[0] in "48" else "SZ")
        return f"{text}.{market}"
    return ""


def code6(code: str) -> str:
    return code.split(".")[0] if code else ""


def clean(value) -> str:
    text = str(value if value is not None else "").strip()
    return "" if text.lower() in {"nan", "none", "null", "-"} else text


# --------------------------------------------------------------------------- #
# 状态台账
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"updated_at": None, "stocks": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "stocks": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def save_state_merged(state: dict, code: str) -> None:
    """只把本进程对某只票的更新合并进磁盘台账，避免覆盖并发进程（promote/news 等）对其他票的更新。"""
    disk = load_state()
    disk.setdefault("stocks", {})[code] = (state.get("stocks") or {}).get(code) or {}
    save_state(disk)


def targets_from_bynewlt() -> dict[str, str]:
    """默认目标：Bynewlt 台账里 not_found 的票 → {code: name}。"""
    if not BYNEWLT_STATE.exists():
        return {}
    try:
        data = json.loads(BYNEWLT_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for code, record in (data.get("stocks") or {}).items():
        if str(record.get("status")) == "not_found":
            out[code] = clean(record.get("name"))
    return out


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    return session


def http_get(session: requests.Session, url: str, *, tries: int = 2,
             method: str = "GET", **kwargs) -> requests.Response | None:
    kwargs.setdefault("timeout", 20)
    for attempt in range(tries):
        try:
            if method.upper() == "POST":
                return session.post(url, **kwargs)
            return session.get(url, **kwargs)
        except requests.RequestException:  # noqa: PERF203 - 失败后退避一次
            if attempt + 1 < tries:
                time.sleep(1.2)
    return None


def _json_or_empty(response) -> dict:
    if response is None:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


# --- PDF 反爬挑战（EO_Bot / __tst_status） ---------------------------------- #

def _solve_cookies_with_node(script_js: str) -> dict[str, str]:
    """用 node 执行挑战脚本，捕获 document.cookie 赋值（环境无 node 时返回空）。"""
    node = shutil.which("node")
    if not node:
        return {}
    preamble = (
        "const __cookies = {};\n"
        "const document = { set cookie(v) {\n"
        "  (v || '').split(';').forEach(function (p) { p = p.trim(); if (!p) return;\n"
        "    var i = p.indexOf('='); if (i > 0) __cookies[p.slice(0, i)] = p.slice(i + 1); });\n"
        "}, get cookie() { return ''; } };\n"
        "globalThis.setTimeout = function () {};\n"
        "globalThis.location = { href: '' };\n"
        "globalThis.window = globalThis;\n"
    )
    payload = preamble + script_js + "\nconsole.log(JSON.stringify(__cookies));\n"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(payload)
            path = handle.name
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=20)
        text = (proc.stdout or "").strip().splitlines()
        if text:
            return {str(k): str(v) for k, v in json.loads(text[-1]).items()}
    except Exception:  # noqa: BLE001 - 求解失败不致命，单 PDF 会如实记错
        pass
    finally:
        try:
            os.unlink(path)  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
    return {}


def solve_pdf_challenge(html: str) -> dict[str, str]:
    match = re.search(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
    if not match:
        return {}
    return _solve_cookies_with_node(match.group(1))


def load_pdf_cookies(session: requests.Session) -> None:
    if COOKIE_PATH.exists():
        try:
            for key, value in json.loads(COOKIE_PATH.read_text(encoding="utf-8")).items():
                session.cookies.set(str(key), str(value))
        except (OSError, json.JSONDecodeError):
            pass


def save_pdf_cookies(session: requests.Session) -> None:
    try:
        cookies = {c.name: c.value for c in session.cookies}
        COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def fetch_pdf_bytes(session: requests.Session, url: str, referer: str) -> bytes | None:
    """下载 PDF；遇 JS 挑战就地解 cookie 后重试一次。"""
    response = http_get(session, url, headers={"Referer": referer}, tries=1)
    if response is None:
        return None
    if response.content[:4] == b"%PDF":
        return response.content
    if b"EO_Bot_Ssid" in response.content or b"__tst_status" in response.content:
        html = response.content.decode("utf-8", errors="replace")
        cookies = solve_pdf_challenge(html)
        if cookies:
            for key, value in cookies.items():
                session.cookies.set(key, value)
            save_pdf_cookies(session)
            response = http_get(session, url, headers={"Referer": referer}, tries=1)
            if response is not None and response.content[:4] == b"%PDF":
                return response.content
    return None


def pdf_pages_text(content: bytes, *, page_cap: int = 8, page_chars: int = REPORT_PAGE_CHARS):
    """pypdf 逐页抽文本；返回 (页数, [逐页文本])。"""
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    total = len(reader.pages)
    pages = []
    for index in range(min(total, page_cap)):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页失败不算致命
            text = ""
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        pages.append(text[:page_chars])
    return total, pages


# --------------------------------------------------------------------------- #
# 证据来源 1：东财研报（列表 + PDF 全文）
# --------------------------------------------------------------------------- #

def fetch_eastmoney_reports(session: requests.Session, code: str) -> list[dict]:
    begin = (datetime.now(CST) - timedelta(days=REPORT_MONTHS * 31)).strftime("%Y-%m-%d")
    response = http_get(session, "https://reportapi.eastmoney.com/report/list",
                        params={"industryCode": "*", "pageSize": "100", "industry": "*",
                                "rating": "*", "ratingChange": "*", "beginTime": begin,
                                "endTime": "2030-01-01", "pageNo": "1", "qType": "0",
                                "code": code},
                        headers={"Referer": "https://data.eastmoney.com/"})
    if response is None:
        return []
    try:
        rows = response.json().get("data") or []
    except ValueError:
        return []
    items = []
    for row in rows[:REPORT_MAX_LIST]:
        items.append({
            "org": clean(row.get("orgSName")),
            "title": clean(row.get("title")),
            "date": clean(row.get("publishDate"))[:10],
            "rating": clean(row.get("emRatingName")),
            "rating_change": clean(row.get("lastEmRatingName")),
            "eps_this_year": row.get("predictThisYearEps"),
            "eps_next_year": row.get("predictNextYearEps"),
            "eps_next_two_year": row.get("predictNextTwoYearEps"),
            "aim_low": row.get("indvAimPriceL"),
            "aim_target": row.get("indvAimPriceT"),
            "author": clean(row.get("author")),
            "info_code": clean(row.get("infoCode")),
            "industry": clean(row.get("indvInduName")),
        })
    return items


# --------------------------------------------------------------------------- #
# 证据来源 2：巨潮公告（列表 + PDF 全文）
# --------------------------------------------------------------------------- #

def cninfo_org_id(session: requests.Session, code: str) -> str:
    response = http_get(session, "https://www.cninfo.com.cn/new/information/topSearch/query",
                        method="POST", tries=1,
                        data={"keyWord": code, "maxNum": "10"},
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Referer": "https://www.cninfo.com.cn/"})
    if response is None:
        return ""
    try:
        rows = response.json()
    except ValueError:
        return ""
    for row in rows or []:
        if clean(row.get("code")) == code:
            return clean(row.get("orgId"))
    return ""


def fetch_cninfo_announcements(session: requests.Session, code: str) -> list[dict]:
    org_id = cninfo_org_id(session, code)
    if not org_id:
        return []
    response = http_get(session, "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                        method="POST", tries=1,
                        data={"stock": f"{code},{org_id}", "tabName": "fulltext",
                              "pageSize": str(ANNO_MAX_LIST), "pageNum": "1", "column": "",
                              "category": "", "plate": "", "seDate": "", "searchkey": "",
                              "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"},
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Referer": "https://www.cninfo.com.cn/new/disclosure",
                                 "Origin": "https://www.cninfo.com.cn"})
    if response is None:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    items = []
    for row in payload.get("announcements") or []:
        stamp = row.get("announcementTime")
        date = (datetime.fromtimestamp(stamp / 1000, tz=CST).strftime("%Y-%m-%d")
                if isinstance(stamp, (int, float)) else clean(stamp)[:10])
        adjunct = clean(row.get("adjunctUrl"))
        items.append({
            "title": clean(row.get("announcementTitle")),
            "date": date,
            "url": (f"https://static.cninfo.com.cn/{adjunct}" if adjunct
                    else f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={clean(row.get('announcementId'))}"),
            "pdf": adjunct.endswith(".PDF") or adjunct.endswith(".pdf"),
        })
    return items


def anno_priority(item: dict) -> int:
    title = str(item.get("title"))
    for index, keyword in enumerate(ANNO_KEYWORDS):
        if keyword in title:
            return index
    return len(ANNO_KEYWORDS)


# --------------------------------------------------------------------------- #
# 证据来源 3：searxng 搜索 + 新闻页正文
# --------------------------------------------------------------------------- #

def searxng_search(session: requests.Session, query: str, limit: int) -> tuple[list[dict], list]:
    response = http_get(session, f"{SEARXNG_URL}/search",
                        params={"q": query, "format": "json", "language": "zh-CN"},
                        tries=1, timeout=25)
    if response is None:
        return [], []
    try:
        payload = response.json()
    except ValueError:
        return [], []
    results = []
    for row in (payload.get("results") or [])[:limit]:
        results.append({
            "title": clean(row.get("title")),
            "url": clean(row.get("url")),
            "snippet": clean(row.get("content"))[:360],
            "engine": clean(row.get("engine")),
        })
    return results, payload.get("unresponsive_engines") or []


def build_news_queries(name: str, code: str) -> list[str]:
    base = f"{name} {code}"
    return [
        f"{base} 研报",
        f"{name} 业绩 财报 最新",
        f"{name} 订单 中标 合作 产能",
        f"{name} 风险 诉讼 处罚 减持",
        f"{base} 机构调研",
    ][:NEWS_QUERIES]


def fetch_page_text(session: requests.Session, url: str, *, chars: int = NEWS_TEXT_CHARS) -> dict:
    """抓静态页正文；JS 渲染的空壳页如实按失败记录。"""
    response = http_get(session, url, tries=1, timeout=18)
    if response is None:
        return {"ok": False, "error": "request_failed", "text": ""}
    if response.status_code >= 400:
        return {"ok": False, "error": f"http_{response.status_code}", "text": ""}
    from bs4 import BeautifulSoup

    html = response.text
    if not html:
        return {"ok": False, "error": "empty", "text": ""}
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        tag.decompose()
    text = re.sub(r"\n{2,}", "\n", soup.get_text("\n", strip=True))
    if len(text) < 200:
        return {"ok": False, "error": "js_shell", "text": ""}
    title = clean(soup.title.get_text(strip=True) if soup.title else "")[:160]
    date_match = re.search(r"20\d{2}[-/年]\s?\d{1,2}[-/月]\s?\d{1,2}", text[:1500])
    return {"ok": True, "error": "", "title": title,
            "date": (date_match.group(0).replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").strip() if date_match else ""),
            "text": text[:chars]}


def fetch_news_pack(session: requests.Session, name: str, code: str) -> tuple[list[dict], list, list, int]:
    """searxng 多查询 → 相关性过滤（只留命中股票名/代码的结果）→ 抓正文。

    中文查询下 bing 等引擎常见无关噪声；360search 被限流时宁可降级为空，
    也不把噪声当证据。返回 (news, unresponsive, shortlist, dropped)。
    """
    queries = build_news_queries(name, code)
    seen: set[str] = set()
    collected: list[dict] = []
    unresponsive: list = []
    dropped = 0
    for query in queries:
        results, dead = searxng_search(session, query, NEWS_MAX_RESULTS)
        unresponsive.extend(dead)
        for row in results:
            url = row["url"]
            if not url or url in seen:
                continue
            seen.add(url)
            text = f"{row['title']} {row['snippet']}"
            relevant = (name and name in text) or (code in text) or (code in url)
            if not relevant:
                dropped += 1
                continue
            if len(collected) >= NEWS_KEEP_MAX:
                continue
            collected.append({**row, "query": query, "relevant": True})
        time.sleep(2.5)
    # 抓正文：优先域名优先级
    def sort_key(item: dict):
        domain = re.sub(r"^https?://([^/]+).*$", r"\1", item["url"])
        domain_rank = NEWS_DOMAIN_PRIORITY.index(domain) if domain in NEWS_DOMAIN_PRIORITY else 99
        return domain_rank

    shortlist = sorted(collected, key=sort_key)[:NEWS_FETCH_MAX]
    for item in shortlist:
        page = fetch_page_text(session, item["url"])
        item["text_ok"] = page["ok"]
        item["text"] = page.get("text", "")
        if page.get("date") and not item.get("date"):
            item["date"] = page["date"]
        time.sleep(1.0)
    for item in collected:
        item.setdefault("text_ok", False)
        item.setdefault("text", "")
    return collected, unresponsive, shortlist, dropped


# --------------------------------------------------------------------------- #
# 证据来源 4：行情与资金面
# --------------------------------------------------------------------------- #

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def eastmoney_datacenter(session: requests.Session, report_name: str, *, filter_str: str = "",
                         page_size: int = 30, sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    response = http_get(session, DATACENTER_URL, tries=1,
                        params={"reportName": report_name, "columns": "ALL", "filter": filter_str,
                                "pageNumber": "1", "pageSize": str(page_size),
                                "sortColumns": sort_columns, "sortTypes": sort_types,
                                "source": "WEB", "client": "WEB"})
    if response is None:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    result = payload.get("result") or {}
    return result.get("data") or []


def tencent_quote(session: requests.Session, code: str) -> dict:
    prefix = "sh" if code[0] in "69" else ("bj" if code[0] in "48" else "sz")
    response = http_get(session, f"https://qt.gtimg.cn/q={prefix}{code}", tries=1)
    if response is None:
        return {}
    try:
        text = response.content.decode("gbk", errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    if '"' not in text:
        return {}
    values = text.split('"')[1].split("~")
    if len(values) < 53:
        return {}

    def number(index: int) -> float:
        try:
            return float(values[index] or 0)
        except (ValueError, IndexError):
            return 0.0

    return {"name": clean(values[1]), "price": number(3), "change_pct": number(32),
            "turnover_pct": number(38), "pe_ttm": number(39), "mcap_yi": number(44),
            "float_mcap_yi": number(45), "pb": number(46)}


def fetch_quant_context(session: requests.Session, code: str) -> dict:
    out: dict = {"errors": []}

    def step(name: str, func, *args, **kwargs):
        try:
            out[name] = func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            out[name] = None
            out["errors"].append(f"{name}: {exc}")

    prefix = 1 if code[0] in "69" else 0

    def fund_flow() -> list[dict]:
        response = http_get(session, "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                            tries=1,
                            params={"secid": f"{prefix}.{code}", "fields1": "f1,f2,f3,f7",
                                    "fields2": "f51,f52,f53,f54,f55,f56,f57", "lmt": "120"},
                            headers={"Referer": "https://quote.eastmoney.com/"})
        rows = []
        for line in _json_or_empty(response).get("data", {}).get("klines") or []:
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    rows.append({"date": parts[0], "main_net": float(parts[1])})
                except ValueError:
                    continue
        return rows

    step("fund_flow_120d", fund_flow)
    step("lhb_90d", lambda: [
        {"date": clean(row.get("TRADE_DATE"))[:10], "reason": clean(row.get("EXPLANATION")),
         "net_buy_wan": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1)}
        for row in eastmoney_datacenter(session, "RPT_DAILYBILLBOARD_DETAILSNEW",
                                        filter_str=(f"(SECURITY_CODE=\"{code}\")"
                                                    f"(TRADE_DATE>='{(datetime.now(CST) - timedelta(days=90)).strftime('%Y-%m-%d')}')"),
                                        page_size=30, sort_columns="TRADE_DATE")])
    step("margin_5", lambda: [
        {"date": clean(row.get("DATE"))[:10], "rzye_yi": round((row.get("RZYE") or 0) / 1e8, 2),
         "rqye_yi": round((row.get("RQYE") or 0) / 1e8, 3), "rzrqye_yi": round((row.get("RZRQYE") or 0) / 1e8, 2)}
        for row in eastmoney_datacenter(session, "RPTA_WEB_RZRQ_GGMX",
                                        filter_str=f'(SCODE="{code}")', page_size=5, sort_columns="DATE")])
    step("lockup_180d", lambda: [
        {"date": clean(row.get("FREE_DATE"))[:10], "type": clean(row.get("LIMITED_STOCK_TYPE")),
         "shares": row.get("FREE_SHARES_NUM"), "ratio": row.get("FREE_RATIO")}
        for row in eastmoney_datacenter(session, "RPT_LIFT_STAGE",
                                        filter_str=(f'(SECURITY_CODE="{code}")'
                                                    f"(FREE_DATE>='{datetime.now(CST).strftime('%Y-%m-%d')}')"
                                                    f"(FREE_DATE<='{(datetime.now(CST) + timedelta(days=180)).strftime('%Y-%m-%d')}')"),
                                        page_size=20, sort_columns="FREE_DATE", sort_types="1")])
    step("holder_num_4q", lambda: [
        {"date": clean(row.get("END_DATE"))[:10], "holder_num": row.get("HOLDER_NUM"),
         "change_ratio": row.get("HOLDER_NUM_RATIO"), "avg_shares": row.get("AVG_FREE_SHARES")}
        for row in eastmoney_datacenter(session, "RPT_HOLDERNUMLATEST",
                                        filter_str=f'(SECURITY_CODE="{code}")', page_size=4, sort_columns="END_DATE")])
    step("dividend_3", lambda: [
        {"date": clean(row.get("EX_DIVIDEND_DATE"))[:10], "bonus_rmb": row.get("PRETAX_BONUS_RMB"),
         "transfer": row.get("TRANSFER_RATIO"), "plan": clean(row.get("ASSIGN_PROGRESS"))}
        for row in eastmoney_datacenter(session, "RPT_SHAREBONUS_DET",
                                        filter_str=f'(SECURITY_CODE="{code}")', page_size=3,
                                        sort_columns="EX_DIVIDEND_DATE")])
    def stock_info() -> dict:
        response = http_get(session, "https://push2.eastmoney.com/api/qt/stock/get", tries=1,
                            params={"fltt": "2", "invt": "2",
                                    "fields": "f57,f58,f84,f85,f116,f117,f127,f189",
                                    "secid": f"{prefix}.{code}"})
        data = _json_or_empty(response).get("data") or {}
        return {"industry": clean(data.get("f127")), "list_date": clean(data.get("f189"))}

    step("stock_info", stock_info)
    return out


# --------------------------------------------------------------------------- #
# 证据来源 5：Tushare 结构化数据（券商预测/调研/回购/解禁/两融/股东/分红/筹码）
# --------------------------------------------------------------------------- #

TUSHARE_URL = os.environ.get("TUSHARE_API_URL", "https://api.tushare.pro")
_TUSHARE_BROKER_CACHE: dict[str, list] = {}


def _tushare_call(api: str, params: dict, *, timeout: int = 25) -> tuple[list, str]:
    """直连 Tushare Pro HTTP API；返回 (rows, error)。token 只从环境读取，绝不打印。"""
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        return [], f"{api}: missing TUSHARE_TOKEN"
    try:
        response = requests.post(TUSHARE_URL, timeout=timeout,
                                 json={"api_name": api, "token": token, "params": params, "fields": ""})
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return [], f"{api}: {exc}"
    if payload.get("code") != 0:
        return [], f"{api}: {clean(payload.get('msg'))[:90]}"
    data = payload.get("data") or {}
    fields = data.get("fields") or []
    return [dict(zip(fields, item)) for item in (data.get("items") or [])], ""


def _dedupe_report_rc(rows: list[dict]) -> list[dict]:
    """report_rc 每份研报按预测年份拆多行 → 每份报告取一行（首预测年）。"""
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (clean(row.get("org_name")), clean(row.get("report_date")), clean(row.get("report_title")))
        grouped.setdefault(key, []).append(row)
    items = []
    for (org, date, title), group in grouped.items():
        group.sort(key=lambda r: str(r.get("quarter") or "9999"))
        head = group[0]

        def first(field):
            return next((r.get(field) for r in group if r.get(field) is not None), None)

        items.append({
            "org": org, "date": date, "title": title,
            "author": clean(head.get("author_name")),
            "rating": clean(head.get("rating")),
            "eps": first("eps"), "pe": first("pe"), "np_wan": first("np"),
            "max_price": first("max_price"), "min_price": first("min_price"),
            "forecast_period": clean(head.get("quarter")),
        })
    items.sort(key=lambda r: r["date"], reverse=True)
    return items[:20]


def fetch_tushare_context(code: str) -> dict:
    """Tushare 结构化数据层；任何子接口失败只记账不抛错。"""
    now = datetime.now(CST)
    start_18m = (now - timedelta(days=540)).strftime("%Y%m%d")
    start_45d = (now - timedelta(days=45)).strftime("%Y%m%d")
    month = now.strftime("%Y%m")
    out: dict = {"errors": []}

    def step(name: str, api: str, params: dict, process) -> None:
        rows, error = _tushare_call(api, params)
        if error:
            out[name] = []
            out["errors"].append(error)
        else:
            try:
                out[name] = process(rows)
            except Exception as exc:  # noqa: BLE001
                out[name] = []
                out["errors"].append(f"{name}: {exc}")
        time.sleep(0.4)

    step("report_rc", "report_rc", {"ts_code": code, "start_date": start_18m}, _dedupe_report_rc)

    def _surv(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("surv_date")), reverse=True)[:5]
        return [{"date": clean(r.get("surv_date")), "mode": clean(r.get("rece_mode")),
                 "place": clean(r.get("rece_place")), "orgs": clean(r.get("rece_org"))[:120],
                 "org_type": clean(r.get("org_type"))} for r in rows]

    step("surv", "stk_surv", {"ts_code": code, "start_date": start_18m}, _surv)

    def _repurchase(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("ann_date")), reverse=True)[:6]
        return [{"ann_date": clean(r.get("ann_date")), "proc": clean(r.get("proc")),
                 "vol_wan": round((r.get("vol") or 0) / 1e4, 1),
                 "amount_yi": round((r.get("amount") or 0) / 1e8, 3),
                 "high": r.get("high_limit"), "low": r.get("low_limit")} for r in rows]

    step("repurchase", "repurchase", {"ts_code": code}, _repurchase)

    def _share_float(rows: list[dict]) -> list[dict]:
        today = now.strftime("%Y%m%d")
        future = sorted((r for r in rows if clean(r.get("float_date")) >= today),
                        key=lambda r: clean(r.get("float_date")))
        return [{"float_date": clean(r.get("float_date")),
                 "float_share_wan": round((r.get("float_share") or 0) / 1e4, 1),
                 "float_ratio": r.get("float_ratio"), "holder": clean(r.get("holder_name"))[:60],
                 "share_type": clean(r.get("share_type"))} for r in future[:8]]

    step("share_float_upcoming", "share_float", {"ts_code": code}, _share_float)

    def _margin(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("trade_date")))[-6:]
        return [{"trade_date": clean(r.get("trade_date")),
                 "rzye_yi": round((r.get("rzye") or 0) / 1e8, 3),
                 "rqye_yi": round((r.get("rqye") or 0) / 1e8, 4),
                 "rzrqye_yi": round((r.get("rzrqye") or 0) / 1e8, 3)} for r in rows]

    step("margin", "margin_detail", {"ts_code": code, "start_date": start_45d}, _margin)

    def _holders(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("end_date")))[-4:]
        return [{"end_date": clean(r.get("end_date")), "ann_date": clean(r.get("ann_date")),
                 "holder_num": r.get("holder_num")} for r in rows]

    step("holdernumber", "stk_holdernumber", {"ts_code": code, "start_date": start_18m}, _holders)

    def _dividend(rows: list[dict]) -> list[dict]:
        rows = [r for r in rows if clean(r.get("div_proc")) in ("实施", "预案", "股东大会通过")]
        seen: set[str] = set()
        items = []
        for row in sorted(rows, key=lambda r: clean(r.get("end_date")), reverse=True):
            key = clean(row.get("end_date"))
            if not key or key in seen:
                continue
            seen.add(key)
            items.append({"end_date": key, "ann_date": clean(row.get("ann_date")),
                          "proc": clean(row.get("div_proc")), "cash_div": row.get("cash_div"),
                          "ex_date": clean(row.get("ex_date"))})
            if len(items) >= 4:
                break
        return items

    step("dividend", "dividend", {"ts_code": code}, _dividend)

    def _cyq(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("trade_date")))[-3:]
        return [{"trade_date": clean(r.get("trade_date")), "winner_rate": r.get("winner_rate"),
                 "weight_avg": r.get("weight_avg"), "cost_15pct": r.get("cost_15pct"),
                 "cost_50pct": r.get("cost_50pct"), "cost_85pct": r.get("cost_85pct")} for r in rows]

    step("cyq_perf", "cyq_perf", {"ts_code": code, "start_date": start_45d}, _cyq)

    def _forecast(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("ann_date")), reverse=True)[:4]
        return [{"ann_date": clean(r.get("ann_date")), "end_date": clean(r.get("end_date")),
                 "type": clean(r.get("type")), "p_change_min": r.get("p_change_min"),
                 "p_change_max": r.get("p_change_max"), "summary": clean(r.get("summary"))[:150]}
                for r in rows]

    step("forecast", "forecast", {"ts_code": code, "start_date": start_18m}, _forecast)

    def _express(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: clean(r.get("ann_date")), reverse=True)[:4]
        return [{"ann_date": clean(r.get("ann_date")), "end_date": clean(r.get("end_date")),
                 "revenue_yi": round((r.get("revenue") or 0) / 1e8, 3) if r.get("revenue") is not None else None,
                 "n_income_yi": round((r.get("n_income") or 0) / 1e8, 3) if r.get("n_income") is not None else None,
                 "yoy_net_profit": r.get("yoy_net_profit")} for r in rows]

    step("express", "express", {"ts_code": code, "start_date": start_18m}, _express)

    # 券商金股（按月列表缓存，本进程内只调一次）
    cache = _TUSHARE_BROKER_CACHE.get(month)
    if cache is None:
        cache, error = _tushare_call("broker_recommend", {"month": month})
        if error:
            cache = []
            out["errors"].append(error)
        _TUSHARE_BROKER_CACHE[month] = cache
    hits = [clean(row.get("broker")) for row in cache if clean(row.get("ts_code")) == code]
    out["broker_recommend"] = [{"month": month, "brokers": hits}] if hits else []
    return out


def _fmt_date(value) -> str:
    text = clean(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def build_tushare_facts(pack: dict) -> list[tuple[str, str]]:
    """Tushare 层 → D 编号事实行（提示词与引文核对共用同一份编号）。"""
    data = pack.get("tushare") or {}
    facts: list[tuple[str, str]] = []

    def add(text: str) -> None:
        facts.append((f"D{len(facts) + 1}", text))

    for item in (data.get("report_rc") or [])[:14]:
        eps = f"，EPS(首年) {item.get('eps')}" if item.get("eps") is not None else ""
        prices = [p for p in (item.get("max_price"), item.get("min_price")) if p]
        target = f"，目标价 {'-'.join(str(p) for p in prices)} 元" if prices else ""
        add(f"券商预测：{item.get('org', '')} {_fmt_date(item.get('date'))} 评级「{item.get('rating') or '—'}」"
            f"{eps}{target}｜《{str(item.get('title') or '')[:48]}》")
    for item in (data.get("repurchase") or []):
        add(f"回购：{_fmt_date(item.get('ann_date'))} {item.get('proc', '')} 已回购 {item.get('vol_wan', '')} 万股"
            f"/金额 {item.get('amount_yi', '')} 亿元（价格区间 {item.get('low', '')}-{item.get('high', '')} 元）")
    for item in (data.get("share_float_upcoming") or []):
        add(f"解禁：{_fmt_date(item.get('float_date'))} {item.get('holder', '')} 解禁 {item.get('float_share_wan', '')} 万股"
            f" 占比 {item.get('float_ratio', '')}%（{item.get('share_type', '')}）")
    for item in (data.get("cyq_perf") or [])[-1:]:
        add(f"筹码分布（{_fmt_date(item.get('trade_date'))}）：获利盘 {item.get('winner_rate', '')}%，"
            f"平均成本 {item.get('weight_avg', '')} 元，50 分位成本 {item.get('cost_50pct', '')} 元")
    for item in (data.get("surv") or [])[:3]:
        add(f"机构调研：{_fmt_date(item.get('date'))} {item.get('mode', '')}｜接待对象：{item.get('orgs', '')[:80]}")
    for item in (data.get("margin") or [])[-1:]:
        add(f"两融：{_fmt_date(item.get('trade_date'))} 融资余额 {item.get('rzye_yi', '')} 亿元，"
            f"融券余额 {item.get('rqye_yi', '')} 亿元")
    for item in (data.get("holdernumber") or [])[-1:]:
        add(f"股东户数：{_fmt_date(item.get('end_date'))} {item.get('holder_num', '')} 户")
    for item in (data.get("dividend") or [])[:1]:
        add(f"分红：报告期 {_fmt_date(item.get('end_date'))} 每股派现 {item.get('cash_div', '')} 元（{item.get('proc', '')}）")
    for item in (data.get("broker_recommend") or []):
        add(f"券商金股：{item.get('month', '')} 推荐券商 {', '.join(item.get('brokers') or [])}")
    for item in (data.get("forecast") or [])[:2]:
        add(f"业绩预告：{_fmt_date(item.get('ann_date'))} {item.get('type', '')} 净利同比 "
            f"{item.get('p_change_min', '')}~{item.get('p_change_max', '')}%｜{item.get('summary', '')}")
    for item in (data.get("express") or [])[:2]:
        add(f"业绩快报：{_fmt_date(item.get('ann_date'))} 营收 {item.get('revenue_yi', '')} 亿元，"
            f"归母净利 {item.get('n_income_yi', '')} 亿元")
    return facts


# --------------------------------------------------------------------------- #
# 单票证据采集
# --------------------------------------------------------------------------- #

def collect_evidence(code: str, name: str, *, download_pdfs: bool = True, verbose: bool = True) -> dict:
    session = make_session()
    load_pdf_cookies(session)
    pack: dict = {
        "code": code, "name": name, "market": "CN", "fetched_at": now_iso(),
        "source": "public_web",
        "sources": {}, "reports": [], "announcements": [], "news": [],
        "quote": {}, "quant": {},
    }

    def log(message: str) -> None:
        if verbose:
            print(f"    {message}", flush=True)

    # 0) 行情快照先取：缺失名字时用它补齐（新闻查询需要名字）
    quote = tencent_quote(session, code6(code))
    pack["quote"] = quote
    if not name and quote.get("name"):
        name = quote["name"]
    pack["name"] = name

    # 1) 研报
    try:
        reports = fetch_eastmoney_reports(session, code6(code))
        pack["sources"]["reports"] = {"ok": True, "count": len(reports), "error": ""}
    except Exception as exc:  # noqa: BLE001
        reports = []
        pack["sources"]["reports"] = {"ok": False, "count": 0, "error": str(exc)}
    pdf_done = 0
    pdf_failed = 0
    if download_pdfs:
        for index, report in enumerate(reports[:REPORT_PDF_MAX], start=1):
            info_code = report.get("info_code")
            if not info_code:
                continue
            url = f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
            content = fetch_pdf_bytes(session, url, "https://data.eastmoney.com/")
            if not content:
                report["pdf"] = {"ok": False, "error": "download_failed"}
                pdf_failed += 1
                continue
            try:
                pages, page_texts = pdf_pages_text(content)
                report["pdf"] = {"ok": True, "pages": pages, "pages_text": page_texts,
                                 "sha256": hashlib.sha256(content).hexdigest()[:16],
                                 "url": url}
                pdf_done += 1
            except Exception as exc:  # noqa: BLE001
                report["pdf"] = {"ok": False, "error": f"parse: {exc}"}
                pdf_failed += 1
            time.sleep(0.5)
    log(f"研报 {len(reports)} 篇 / 全文 {pdf_done} 份（失败 {pdf_failed}）")
    pack["reports"] = reports
    pack["sources"]["report_pdfs"] = {"ok": pdf_failed == 0, "count": pdf_done, "error": ""}

    # 2) 公告
    try:
        annos = fetch_cninfo_announcements(session, code6(code))
        pack["sources"]["announcements"] = {"ok": True, "count": len(annos), "error": ""}
    except Exception as exc:  # noqa: BLE001
        annos = []
        pack["sources"]["announcements"] = {"ok": False, "count": 0, "error": str(exc)}
    anno_pdf_done = 0
    if download_pdfs and annos:
        ranked = sorted(annos, key=anno_priority)[:ANNO_PDF_MAX]
        for item in ranked:
            try:
                content = http_get(session, item["url"], tries=1, timeout=25)
                if content is None or content.content[:4] != b"%PDF":
                    item["pdf_text"] = None
                    continue
                _, page_texts = pdf_pages_text(content.content, page_cap=5, page_chars=1800)
                item["pdf_text"] = "\n".join(page_texts)[:5200]
                anno_pdf_done += 1
            except Exception:  # noqa: BLE001
                item["pdf_text"] = None
            time.sleep(0.4)
    log(f"公告 {len(annos)} 条 / 全文 {anno_pdf_done} 份")
    pack["announcements"] = annos
    pack["sources"]["announcement_pdfs"] = {"ok": True, "count": anno_pdf_done, "error": ""}

    # 3) 新闻（searxng）
    try:
        news, unresponsive, _, dropped = fetch_news_pack(session, name or code6(code), code6(code))
        news_ok = sum(1 for item in news if item.get("text_ok"))
        engines_down = sorted({str(item[0]) for item in unresponsive if item})
        pack["sources"]["searxng"] = {
            "ok": True, "count": len(news), "fetched": news_ok, "dropped": dropped,
            "degraded": len(news) == 0, "engines_down": engines_down[:6], "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        news = []
        pack["sources"]["searxng"] = {"ok": False, "count": 0, "error": str(exc)}
    log(f"网络线索 {len(news)} 条（正文 {sum(1 for i in news if i.get('text_ok'))} 条）")
    pack["news"] = news

    # 4) 行情 + 资金面
    quote = tencent_quote(session, code6(code))
    pack["quote"] = quote
    if quote.get("name") and not pack["name"]:
        pack["name"] = quote["name"]
    pack["quant"] = fetch_quant_context(session, code6(code))
    log(f"行情 {quote.get('name', '')} {quote.get('price', '')} PE={quote.get('pe_ttm', '')} "
        f"| quant errors={len(pack['quant'].get('errors') or [])}")

    # 5) Tushare 结构化数据（券商预测/调研/回购/解禁/两融/股东/分红/筹码）
    tushare_counts: dict = {}
    try:
        pack["tushare"] = fetch_tushare_context(code)
        tushare_counts = {k: len(v) for k, v in pack["tushare"].items() if isinstance(v, list) and v}
        pack["sources"]["tushare"] = {"ok": True, "counts": tushare_counts,
                                      "errors": (pack["tushare"].get("errors") or [])[:6]}
        log(f"Tushare {tushare_counts or '—'}")
    except Exception as exc:  # noqa: BLE001
        pack["tushare"] = {"errors": [str(exc)]}
        pack["sources"]["tushare"] = {"ok": False, "counts": {}, "errors": [str(exc)]}
        log(f"Tushare 失败: {exc}")

    # 证据可用性判定
    usable = (pdf_done + anno_pdf_done + sum(1 for i in news if i.get("text_ok"))
              + (1 if quote else 0) + len(reports) + len(annos)
              + (1 if tushare_counts else 0))
    pack["usable_items"] = usable
    return pack


def evidence_digest(pack: dict) -> dict:
    reports = pack.get("reports") or []
    annos = pack.get("announcements") or []
    news = pack.get("news") or []
    brokers = sorted({clean(r.get("org")) for r in reports if r.get("org")})
    dates = sorted(clean(r.get("date")) for r in reports if r.get("date"))
    tushare = pack.get("tushare") or {}
    tushare_counts = {k: len(v) for k, v in tushare.items() if isinstance(v, list) and v}
    return {
        "items": len(reports) + len(annos) + len(news),
        "reports": len(reports),
        "report_pdfs": sum(1 for r in reports if (r.get("pdf") or {}).get("ok")),
        "announcements": len(annos),
        "announcement_pdfs": sum(1 for a in annos if a.get("pdf_text")),
        "news": len(news),
        "news_text": sum(1 for n in news if n.get("text_ok")),
        "brokers": brokers,
        "report_dates": [dates[0], dates[-1]] if dates else [],
        "tushare": tushare_counts,
        "fetched_at": pack.get("fetched_at"),
        "source": "public_web",
    }


# --------------------------------------------------------------------------- #
# fetch / status / show / mark
# --------------------------------------------------------------------------- #

def fetch(args) -> int:
    state = load_state()
    stocks = state.setdefault("stocks", {})
    if args.codes:
        wanted = {norm_code(part) for part in args.codes.split(",") if part.strip()}
        targets = {code: (stocks.get(code, {}).get("name", "")) for code in sorted(wanted)}
    else:
        targets = targets_from_bynewlt()
    targets = {code: name for code, name in targets.items() if code}
    if args.limit:
        targets = dict(list(targets.items())[: args.limit])
    todo = []
    for code, name in targets.items():
        record = stocks.get(code) or {}
        if not args.refetch and record.get("status") in (ST_FETCHED, ST_NO_FUNDING):
            continue
        todo.append((code, name or record.get("name", "")))
    print(f"目标 {len(todo)} 只（共 {len(targets)}）；学习节流 sleep={args.sleep}s", flush=True)
    done = 0
    for code, name in todo:
        if not name:
            name = (stocks.get(code) or {}).get("name", "")
        record = stocks.setdefault(code, {})
        record.update({"status": ST_FETCHING, "fetching_at": now_iso()})
        if name:
            record["name"] = name
        save_state_merged(state, code)
        print(f"[{done + 1}/{len(todo)}] {code} {name}", flush=True)
        started = time.time()
        try:
            pack = collect_evidence(code, name)
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            path = EVIDENCE_DIR / f"{code}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            digest = evidence_digest(pack)
            record.update({
                "status": ST_FETCHED if pack["usable_items"] else ST_NO_FUNDING,
                "name": pack.get("name") or name,
                "fetched_at": pack["fetched_at"],
                "evidence_file": f"evidence/{code}.json",
                "digest": digest,
                "sources": pack.get("sources"),
                "elapsed_s": round(time.time() - started, 1),
                "error": "",
            })
            print(f"    -> {record['status']} items={digest['items']} "
                  f"reports={digest['reports']}(full {digest['report_pdfs']}) "
                  f"annos={digest['announcements']}(full {digest['announcement_pdfs']}) "
                  f"news={digest['news']}(text {digest['news_text']}) "
                  f"({record['elapsed_s']}s)", flush=True)
        except Exception as exc:  # noqa: BLE001
            record.update({"status": ST_ERROR, "error": str(exc), "fetched_at": now_iso()})
            print(f"    -> error: {exc}", flush=True)
        save_state_merged(state, code)
        done += 1
        time.sleep(max(0.0, args.sleep))
    print("本批结束。", flush=True)
    return 0


def totals(state: dict) -> dict:
    counts: dict[str, int] = {}
    for record in (state.get("stocks") or {}).values():
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def status(args) -> int:
    state = load_state()
    stocks = state.get("stocks") or {}
    if not stocks:
        print("台账为空：先跑 `fetch`。")
        return 0
    print(f"更新于 {state.get('updated_at')}")
    print("状态汇总:", json.dumps(totals(state), ensure_ascii=False))
    composed = [c for c, r in stocks.items() if r.get("status") == ST_COMPOSED]
    pending = [c for c, r in stocks.items() if r.get("status") == ST_FETCHED]
    print(f"已合成 {len(composed)} 只；已取证据待合成 {len(pending)} 只: {sorted(pending)[:20]}")
    if args.verbose:
        for code in sorted(stocks):
            record = stocks[code]
            digest = record.get("digest") or {}
            print(f"  {code} {record.get('name',''):8s} {record.get('status',''):10s} "
                  f"reports={digest.get('reports','-')}/pdf{digest.get('report_pdfs','-')} "
                  f"annos={digest.get('announcements','-')} news={digest.get('news','-')} "
                  f"{str(record.get('error') or '')[:50]}")
    return 0


def show(args) -> int:
    code = norm_code(args.code)
    path = EVIDENCE_DIR / f"{code}.json"
    if not path.exists():
        print(f"没有证据档：{path}")
        return 1
    pack = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(pack, ensure_ascii=False, indent=1))
        return 0
    digest = evidence_digest(pack)
    print(f"{pack['code']} {pack.get('name','')} | {digest}")
    for report in pack.get("reports") or []:
        pdf = report.get("pdf") or {}
        flag = f"PDF✓{len(pdf.get('pages_text') or [])}页" if pdf.get("ok") else "列表"
        print(f"  [R] {report.get('date')} {report.get('org')} 《{report.get('title')}》 {report.get('rating')} {flag}")
    for anno in (pack.get("announcements") or [])[:12]:
        flag = "全文✓" if anno.get("pdf_text") else "列表"
        print(f"  [A] {anno.get('date')} {anno.get('title')} {flag}")
    for news in (pack.get("news") or [])[:12]:
        flag = "正文✓" if news.get("text_ok") else "摘要"
        print(f"  [N] {news.get('date','')} {news.get('title','')[:60]} {flag} {news.get('url','')[:60]}")
    quote = pack.get("quote") or {}
    print(f"  [Q] {quote.get('name','')} {quote.get('price','')} PE={quote.get('pe_ttm','')} PB={quote.get('pb','')} 市值={quote.get('mcap_yi','')}亿")
    return 0


def news(args) -> int:
    """只补采新闻层（searxng 引擎恢复后重试），不动研报/公告/行情层。"""
    state = load_state()
    stocks = state.get("stocks") or {}
    if args.codes:
        codes = [norm_code(part) for part in args.codes.split(",") if part.strip()]
    else:
        codes = [code for code, record in stocks.items()
                 if record.get("status") in (ST_FETCHED, ST_COMPOSED)]
    codes = [code for code in codes if code]
    if args.limit:
        codes = codes[: args.limit]
    print(f"新闻层补采 {len(codes)} 只", flush=True)
    updated = 0
    for code in codes:
        path = EVIDENCE_DIR / f"{code}.json"
        if not path.exists():
            print(f"  {code}: 无证据档，跳过", flush=True)
            continue
        pack = json.loads(path.read_text(encoding="utf-8"))
        session = make_session()
        name = pack.get("name") or code6(code)
        try:
            items, unresponsive, _, dropped = fetch_news_pack(session, name, code6(code))
        except Exception as exc:  # noqa: BLE001
            print(f"  {code}: 失败 {exc}", flush=True)
            continue
        seen = {item.get("url") for item in items}
        merged = items + [old for old in (pack.get("news") or []) if old.get("url") not in seen]
        pack["news"] = merged[: NEWS_KEEP_MAX + 8]
        news_ok = sum(1 for item in pack["news"] if item.get("text_ok"))
        pack.setdefault("sources", {})["searxng"] = {
            "ok": True, "count": len(pack["news"]), "fetched": news_ok, "dropped": dropped,
            "degraded": len(items) == 0,
            "engines_down": sorted({str(item[0]) for item in unresponsive if item})[:6], "error": "",
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        record = state.setdefault("stocks", {}).setdefault(code, {})
        record["digest"] = evidence_digest(pack)
        record["news_refetched_at"] = now_iso()
        save_state(state)
        updated += 1
        print(f"  {code} {name}: news 共 {len(pack['news'])} 条（正文 {news_ok}）", flush=True)
        time.sleep(max(0.0, args.sleep))
    print(f"完成 {updated} 只。", flush=True)
    return 0


def mark(args) -> int:
    state = load_state()
    code = norm_code(args.code)
    if not code:
        print("缺少/无法识别 --code")
        return 1
    record = state.setdefault("stocks", {}).setdefault(code, {})
    record["status"] = args.status
    record["marked_at"] = now_iso()
    if args.note:
        record["note"] = args.note
    save_state(state)
    print(json.dumps({"code": code, "status": record["status"]}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# draft（LLM 起草 + 引文自动核对）/ promote
# --------------------------------------------------------------------------- #

PROMPT_RULES = """你是严谨的券商研究助理。下面是一只 A 股股票的**公开来源证据资料包**
（券商研报全文节选、公司公告节选、网络新闻线索、行情与资金面数据）。请**只根据资料包内容**，
写一份用于买卖参考的中立多空调研草稿，输出 JSON。

铁律（违反即为废稿）：
1. 不得引入资料包之外的任何信息（包括你不能上网、不能用记忆里的旧闻）；
2. citations[*].quote 必须是资料包原文的**逐字摘录**（允许用"…"省略中间片段），不得改写、不得翻译成外语；
3. 每个 bull/bear/risks 条目的 refs 只能引用资料包中存在的证据编号（R1…R10 研报 / A1… 公告 / N1… 新闻 / D1… Tushare 结构化数据 / Q 行情）；
4. 数字（EPS/评级/目标价/市值/PE/资金流）必须与资料包一致；没有就留空，不许猜；
5. stance 依据证据整体倾向判断：偏多 / 偏空 / 中性 / 证据不足。

JSON 结构：
{
 "stance": "偏多|偏空|中性|证据不足",
 "summary": "≤130 字概括：公司做什么 + 最新业绩/事件 + 核心矛盾",
 "bull":  [{"point": "一句话多头依据", "refs": ["R1"]}, ...],
 "bear":  [{"point": "一句话空头依据", "refs": ["A2"]}, ...],
 "risks": [{"point": "风险与提示", "refs": ["N3"]}, ...],
 "ratings": [{"broker": "机构", "rating": "评级", "target_price": "目标价或空串", "date": "YYYY-MM-DD", "change": "评级调整或空串"}],
 "citations": [{"id": "R1", "quote": "逐字原文", "kind": "report|announcement|news|tushare|quote", "source": "机构名或网站名", "title": "报告/公告/新闻标题或空串", "date": "YYYY-MM-DD 或空串", "url": "https://… 或空串"}],
 "caveats": "一句话说明本稿基于公开来源（非专业研报库）"
}
bull/bear 各 3-6 条，risks 2-4 条；ratings 优先从 D 编号（券商预测）整理、没有可留空数组；citations 覆盖全部被引用编号。"""


def quant_display_lines(pack: dict) -> list[str]:
    """行情与资金面的展示行（提示词与引文核对共用同一份文本）。"""
    quote = pack.get("quote") or {}
    quant = pack.get("quant") or {}
    profile = (quant.get("stock_info") or {})
    rows = [f"行情快照（{pack.get('fetched_at','')}）："
            f"价格 {quote.get('price','')}，PE(TTM) {quote.get('pe_ttm','')}，PB {quote.get('pb','')}，"
            f"总市值 {quote.get('mcap_yi','')} 亿元，行业 {profile.get('industry','') or '—'}，"
            f"上市日期 {profile.get('list_date','') or '—'}。"]
    flow = quant.get("fund_flow_120d") or []
    if flow:
        recent = flow[-20:]
        total = sum(item["main_net"] for item in recent)
        rows.append(f"资金面：近 {len(recent)} 个交易日主力净流入合计 {total/1e8:.2f} 亿元；"
                    f"最近 5 日：" + "，".join(f"{item['date']} {item['main_net']/1e4:.0f}万" for item in flow[-5:]))
    lhb = quant.get("lhb_90d") or []
    if lhb:
        rows.append("近 90 天龙虎榜：" + "；".join(f"{x['date']} {x['reason']} 净买{x['net_buy_wan']}万"
                                               for x in lhb[:5]))
    margin = quant.get("margin_5") or []
    if margin:
        rows.append("两融：" + "；".join(f"{x['date']} 融资余额{x['rzye_yi']}亿 融券{x['rqye_yi']}亿"
                                      for x in margin[:3]))
    lockup = quant.get("lockup_180d") or []
    if lockup:
        rows.append("未来 180 天解禁：" + "；".join(f"{x['date']} {x['type']} 占比{x['ratio']}"
                                              for x in lockup[:4]))
    holders = quant.get("holder_num_4q") or []
    if holders:
        rows.append("股东户数：" + "；".join(f"{x['date']} {x['holder_num']}户 环比{x['change_ratio']}%"
                                       for x in holders[:3]))
    dividends = quant.get("dividend_3") or []
    if dividends:
        rows.append("分红：" + "；".join(f"{x['date']} 每股{x['bonus_rmb']}元 {x['plan']}"
                                     for x in dividends[:2]))
    return rows


def pack_to_prompt(pack: dict) -> str:
    lines: list[str] = []
    lines.append(f"# {pack.get('name','')}（{pack.get('code','')}） 公开来源证据资料包")
    lines.extend(quant_display_lines(pack))

    tushare_facts = build_tushare_facts(pack)
    if tushare_facts:
        lines.append("\n## Tushare 结构化数据（D 编号：券商预测/调研/回购/解禁/筹码/两融/股东/分红）")
        for fid, text in tushare_facts:
            lines.append(f"{fid}｜{text}")

    lines.append("\n## 研报（券商公开研报，R 编号）")
    for index, report in enumerate(pack.get("reports") or [], start=1):
        head = (f"R{index}｜{report.get('org')}｜{report.get('date')}｜《{report.get('title')}》"
                f"｜评级:{report.get('rating') or '—'}｜EPS预测:{report.get('eps_this_year') or '—'}"
                f"/{report.get('eps_next_year') or '—'}｜目标价:{report.get('aim_low') or ''}{report.get('aim_target') or ''}")
        lines.append(head)
        pdf = report.get("pdf") or {}
        if pdf.get("ok"):
            body = "\n".join(f"[第{i+1}页] {page}" for i, page in enumerate(pdf.get("pages_text") or []))
            lines.append(body[:6000])

    lines.append("\n## 公司公告（巨潮，A 编号）")
    for index, anno in enumerate(pack.get("announcements") or [], start=1):
        lines.append(f"A{index}｜{anno.get('date')}｜{anno.get('title')}")
        if anno.get("pdf_text"):
            lines.append(f"[公告全文节选] {str(anno.get('pdf_text'))[:2600]}")

    lines.append("\n## 网络新闻线索（N 编号；'正文'标示是否抓到全文）")
    for index, item in enumerate(pack.get("news") or [], start=1):
        lines.append(f"N{index}｜{item.get('date','')}｜{item.get('title','')}｜{item.get('url','')}")
        if item.get("snippet"):
            lines.append(f"[摘要] {item['snippet']}")
        if item.get("text_ok") and item.get("text"):
            lines.append(f"[正文节选] {str(item['text'])[:1800]}")
    return "\n".join(lines)


def _norm_for_match(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKC", str(text or ""))
    for old, new in [("，", ","), ("。", "."), ("；", ";"), ("：", ":"), ("（", "("), ("）", ")"),
                     ("％", "%"), ("－", "-"), ("—", "-"), ("–", "-"), ("‘", "'"), ("’", "'"),
                     ("“", '"'), ("”", '"'), ("、", ","), ("…", "...")]:
        text = text.replace(old, new)
    text = re.sub(r"[\s\u200b\u00a0]+", "", text)
    return text.lower()


def _norm_for_verify(text: str) -> str:
    """引文核对专用归一：先去掉句读（。；：，、等），再做通用归一。

    这样"逐字"比对不会因模型把「。」改成「；」或跨行拼接而误杀，但数字（含小数点
    "."）、单位、汉字、字母仍必须完全一致。
    """
    text = str(text or "")
    for token in ("。", "；", "：", "，", "、", ";", ":", ",", "…"):
        text = text.replace(token, "")
    return _norm_for_match(text)


def build_searchable_map(pack: dict) -> dict[str, str]:
    """证据编号 → 可检索原文（用于引文逐字核对；句读差异已在归一阶段抹平）。"""
    corpus: dict[str, str] = {}
    for index, report in enumerate(pack.get("reports") or [], start=1):
        pieces = [report.get("title", ""), report.get("org", "")]
        pdf = report.get("pdf") or {}
        for page in pdf.get("pages_text") or []:
            pieces.append(page)
        corpus[f"R{index}"] = _norm_for_verify("\n".join(pieces))
    for index, anno in enumerate(pack.get("announcements") or [], start=1):
        corpus[f"A{index}"] = _norm_for_verify(anno.get("title", "") + "\n" + str(anno.get("pdf_text") or ""))
    for index, item in enumerate(pack.get("news") or [], start=1):
        corpus[f"N{index}"] = _norm_for_verify(
            item.get("title", "") + "\n" + item.get("snippet", "") + "\n" + str(item.get("text") or ""))
    quote = pack.get("quote") or {}
    corpus["Q"] = _norm_for_verify(json.dumps(quote, ensure_ascii=False) + "\n" + "\n".join(quant_display_lines(pack)))
    for fid, text in build_tushare_facts(pack):
        corpus[fid] = _norm_for_verify(f"{fid}｜{text}\n{text}")
    return corpus


def verify_citations(pack: dict, draft: dict) -> dict:
    """逐条核对：citation.quote 拆段后必须都能在对应证据原文中找到；返回核对结果。"""
    corpus = build_searchable_map(pack)
    report = {"checked": 0, "verified": 0, "failed": [], "checked_at": now_iso()}
    citations = []
    refs_valid = {}
    for index, report_item in enumerate(pack.get("reports") or [], start=1):
        refs_valid[f"R{index}"] = True
    for index in range(1, len(pack.get("announcements") or []) + 1):
        refs_valid[f"A{index}"] = True
    for index in range(1, len(pack.get("news") or []) + 1):
        refs_valid[f"N{index}"] = True
    refs_valid["Q"] = True

    for citation in draft.get("citations") or []:
        cid = clean(citation.get("id"))
        quote = str(citation.get("quote") or "")
        report["checked"] += 1
        target = corpus.get(cid)
        if not cid or target is None or not quote:
            report["failed"].append({"id": cid, "reason": "missing_corpus_or_quote"})
            continue
        segments = [segment for segment in re.split(r"(?:\.{3,}|…+)", quote) if segment.strip()]
        normalized = [_norm_for_verify(segment) for segment in segments]
        ok = all(segment and segment in target for segment in normalized)
        if not ok:
            report["failed"].append({"id": cid, "reason": "quote_not_found", "quote": quote[:80]})
            continue
        item = dict(citation)
        # 页号定位（研报 PDF 才有）
        page = None
        if cid.startswith("R") and cid[1:].isdigit():
            report_item = (pack.get("reports") or [])[int(cid[1:]) - 1]
            for page_index, page_text in enumerate(((report_item.get("pdf") or {}).get("pages_text") or []), start=1):
                if _norm_for_match(segments[0]) in _norm_for_match(page_text):
                    page = page_index
                    break
        if page:
            item["page"] = page
        item["verified"] = "cite"
        citations.append(item)
        report["verified"] += 1

    # refs 合法性：把不存在/未核验的 refs 从要点里剔掉
    valid_ids = {c["id"] for c in citations}
    for section in ("bull", "bear", "risks"):
        kept = []
        for point in draft.get(section) or []:
            refs = [clean(r) for r in (point.get("refs") or []) if clean(r) in valid_ids]
            if refs:
                kept.append({"point": clean(point.get("point")), "refs": refs})
        draft[section] = kept
    draft["citations"] = citations
    draft["verification"] = {
        "level": "auto_checked",
        "method": "quote-in-evidence 子串逐条比对（含 … 分段）+ refs 合法性过滤",
        "checked": report["checked"],
        "verified": report["verified"],
        "failed": report["failed"][:20],
        "checked_at": report["checked_at"],
    }
    return draft


def call_deepseek(prompt: str, timeout: int = 240) -> tuple[dict | None, str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return None, "missing DEEPSEEK_API_KEY"
    response = requests.post(DEEPSEEK_URL, timeout=timeout,
                             headers={"Authorization": f"Bearer {key}",
                                      "Content-Type": "application/json"},
                             json={"model": DEEPSEEK_MODEL, "temperature": 0.2,
                                   "response_format": {"type": "json_object"},
                                   "messages": [{"role": "system", "content": PROMPT_RULES},
                                                {"role": "user", "content": prompt}]})
    if response.status_code != 200:
        return None, f"http_{response.status_code}: {response.text[:200]}"
    payload = response.json()
    content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    try:
        return json.loads(content), ""
    except json.JSONDecodeError:
        return None, f"bad_json: {content[:200]}"


def draft(args) -> int:
    state = load_state()
    codes = [norm_code(part) for part in (args.codes or args.code or "").split(",") if part.strip()]
    codes = [code for code in codes if code]
    if not codes:
        codes = [code for code, record in (state.get("stocks") or {}).items()
                 if record.get("status") == ST_FETCHED]
    print(f"起草 {len(codes)} 只：{codes}", flush=True)
    for code in codes:
        pack_path = EVIDENCE_DIR / f"{code}.json"
        if not pack_path.exists():
            print(f"  {code}: 无证据档，跳过", flush=True)
            continue
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        print(f"  {code} {pack.get('name','')}: 生成草稿…", flush=True)
        prompt = pack_to_prompt(pack)
        draft_obj, error = call_deepseek(prompt)
        if draft_obj is None:
            print(f"    -> 失败：{error}", flush=True)
            continue
        draft_obj = verify_citations(pack, draft_obj)
        draft_obj.update({
            "code": code, "name": pack.get("name", ""), "source": "public_web",
            "updated_at": now_iso(),
            "window": f"研报窗口 {REPORT_MONTHS} 个月",
            "evidence_file": f"evidence/{code}.json",
            "verification": draft_obj["verification"],
        })
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        path = DRAFT_DIR / f"{code}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(draft_obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        verification = draft_obj["verification"]
        print(f"    -> 草稿完成：verified {verification['verified']}/{verification['checked']}，"
              f"playbook bull={len(draft_obj.get('bull') or [])} bear={len(draft_obj.get('bear') or [])}", flush=True)
        time.sleep(max(0.0, args.sleep))
    return 0


def promote(args) -> int:
    """agent 复核后：草稿 → dossier（置 agent_checked）；若提供 --drop-citations 先剔除失效引用。"""
    code = norm_code(args.code)
    path = DRAFT_DIR / f"{code}.json"
    if not path.exists():
        print(f"没有草稿：{path}")
        return 1
    dossier = json.loads(path.read_text(encoding="utf-8"))
    verification = dossier.get("verification") or {}
    dossier["verification"] = {
        "level": str(getattr(args, "level", "agent_checked") or "agent_checked"),
        "method": ("自动引文比对 + agent 逐票复核（数值/口径/结论）"
                   if str(getattr(args, "level", "agent_checked")) == "agent_checked"
                   else "自动引文比对（逐字命中原文）；未逐票人工复核"),
        "verified": verification.get("verified"),
        "checked": verification.get("checked"),
        "checked_at": verification.get("checked_at"),
    }
    dossier["promoted_at"] = now_iso()
    DOSSIER_DIR.mkdir(parents=True, exist_ok=True)
    target = DOSSIER_DIR / f"{code}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dossier, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    state = load_state()
    record = state.setdefault("stocks", {}).setdefault(code, {})
    record["status"] = ST_COMPOSED
    record["composed_at"] = now_iso()
    save_state(state)
    print(json.dumps({"code": code, "status": ST_COMPOSED, "dossier": str(target)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P2.73 公开来源个股调研管线（取证据 + 起草）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="逐票采集公开来源证据（免费，耗时长）")
    p_fetch.add_argument("--codes", default="", help="逗号分隔；默认=Bynewlt not_found 票")
    p_fetch.add_argument("--limit", type=int, default=None)
    p_fetch.add_argument("--refetch", action="store_true", help="重采已有终态的票")
    p_fetch.add_argument("--sleep", type=float, default=1.0)
    p_fetch.set_defaults(func=fetch)

    p_status = sub.add_parser("status", help="进度汇总（本地，免费）")
    p_status.add_argument("--verbose", action="store_true")
    p_status.set_defaults(func=status)

    p_show = sub.add_parser("show", help="单票证据摘要（本地，免费）")
    p_show.add_argument("--code", required=True)
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=show)

    p_news = sub.add_parser("news", help="只补采新闻层（引擎恢复后重试；本地免费）")
    p_news.add_argument("--codes", default="")
    p_news.add_argument("--limit", type=int, default=None)
    p_news.add_argument("--sleep", type=float, default=1.0)
    p_news.set_defaults(func=news)

    p_draft = sub.add_parser("draft", help="DeepSeek 起草 + 引文自动核对（产生草稿，不直接发布）")
    p_draft.add_argument("--code", default="", help="单票")
    p_draft.add_argument("--codes", default="", help="逗号分隔多票；缺省=全部 fetched")
    p_draft.add_argument("--sleep", type=float, default=2.0)
    p_draft.set_defaults(func=draft)

    p_promote = sub.add_parser("promote", help="草稿 → dossier（agent 复核后调用）")
    p_promote.add_argument("--code", required=True)
    p_promote.add_argument("--level", default="agent_checked",
                           choices=["auto_checked", "agent_checked"],
                           help="未逐票人工复核的批量草稿请用 auto_checked")
    p_promote.set_defaults(func=promote)

    p_mark = sub.add_parser("mark", help="人工改单票状态（本地，免费）")
    p_mark.add_argument("--code", required=True)
    p_mark.add_argument("--status", required=True,
                        choices=[ST_QUEUED, ST_FETCHING, ST_FETCHED, ST_NO_FUNDING, ST_ERROR, ST_COMPOSED])
    p_mark.add_argument("--note", default="")
    p_mark.set_defaults(func=mark)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
