# 全网热榜聚合 API — 数据获取文档

> 服务地址: `http://<HOST>:3220`
> 覆盖平台: 50+（社交热榜 / 财经快讯 / 科技资讯 / 新闻国际 / 视频娱乐）
> 认证方式: `X-API-Key` header
> 更新日期: 2026-06-07

---

## 一、API 端点

### 公共 Header

所有请求均需携带认证头：

```
X-API-Key: <your_api_key>
```

curl 示例中通过 `$HOTLIST_API_KEY` 环境变量传入，代码中通过配置文件或环境变量注入，**切勿将 key 硬编码或提交到版本控制**。

### 1.1 单平台获取

```bash
GET /api/s?id=<platform_id>
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 平台标识，见下方 [平台列表](#二平台列表) |
| `latest` | 否 | 设为 `true` 时绕过缓存强制刷新 |

**示例：**

```bash
# 微博热搜（命中缓存）
curl -s -H "X-API-Key: $HOTLIST_API_KEY" \
  "$HOTLIST_API_BASE_URL/api/s?id=weibo"

# 财联社电报（强制刷新）
curl -s -H "X-API-Key: $HOTLIST_API_KEY" \
  "$HOTLIST_API_BASE_URL/api/s?id=cls-telegraph&latest=true"
```

### 1.2 批量多平台拉取

```bash
POST /api/s/entire
Content-Type: application/json
```

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `sources` | 是 | `string[]` | 平台 ID 列表，空数组返回全部 |

**示例：**

```bash
curl -s -X POST "$HOTLIST_API_BASE_URL/api/s/entire" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $HOTLIST_API_KEY" \
  -d '{"sources": ["weibo", "zhihu", "cls-telegraph", "wallstreetcn-quick", "36kr-quick"]}'
```

---

## 二、平台列表

### 2.1 社交 / 热榜（13个）

| 平台 ID | 名称 | 说明 |
|---|---|---|
| `weibo` | 微博 | 实时热搜榜，含排名图标 |
| `zhihu` | 知乎 | 热榜 |
| `douyin` | 抖音 | 热点 |
| `bilibili-hot-search` | B站热搜 | 搜索热词 |
| `bilibili-hot-video` | B站热门视频 | 视频排行 |
| `bilibili-ranking` | B站排行榜 | 全站排行榜 |
| `toutiao` | 今日头条 | 热榜 |
| `baidu` | 百度 | 热搜榜 |
| `hupu` | 虎扑 | 步行街热帖 |
| `tieba` | 贴吧 | 热议话题 |
| `kuaishou` | 快手 | 热榜 |
| `thepaper` | 澎湃新闻 | 热榜 |
| `ifeng` | 凤凰网 | 资讯 |
| `nowcoder` | 牛客 | 求职讨论 |
| `freebuf` | FreeBuf | 安全资讯 |
| `douban` | 豆瓣 | 电影/读书热榜 |

### 2.2 财经（10个）

| 平台 ID | 名称 | 说明 |
|---|---|---|
| `cls-telegraph` | 财联社电报 | 实时快讯，含 `pubDate` 时间戳 |
| `cls-depth` | 财联社深度 | 深度研报文章 |
| `cls-hot` | 财联社热榜 | 热门文章 |
| `wallstreetcn-quick` | 华尔街见闻快讯 | 实时短讯 |
| `wallstreetcn-news` | 华尔街见闻新闻 | 新闻文章 |
| `wallstreetcn-hot` | 华尔街见闻热榜 | 热门文章 |
| `gelonghui` | 格隆汇 | 快讯 |
| `xueqiu-hotstock` | 雪球热门个股 | 热门股票讨论 |
| `jin10` | 金十数据 | 快讯 |
| `fastbull-express` | 法布快讯 | 快讯 |
| `fastbull-news` | 法布新闻 | 新闻 |
| `mktnews-flash` | 市场快讯 | 综合市场快讯 |

### 2.3 科技（13个）

| 平台 ID | 名称 | 说明 |
|---|---|---|
| `36kr-quick` | 36氪快讯 | 创投快讯 |
| `36kr-renqi` | 36氪人气榜 | 热门文章 |
| `ithome` | IT之家 | 科技资讯 |
| `coolapk` | 酷安 | 数码热帖 |
| `v2ex-share` | V2EX分享 | 创意分享 |
| `github-trending-today` | GitHub Trending | 今日热门开源项目 |
| `hackernews` | Hacker News | 科技/创业热帖 |
| `producthunt` | Product Hunt | 产品发布热榜 |
| `solidot` | Solidot | 科技奇客资讯 |
| `juejin` | 掘金 | 开发者热文 |
| `sspai` | 少数派 | 数字生活 |
| `aihot` | AI热门 | AI 相关热点聚合 |
| `pcbeta-windows11` | 远景论坛 | Windows 11 讨论 |

### 2.4 新闻 / 国际（6个）

| 平台 ID | 名称 | 说明 |
|---|---|---|
| `zaobao` | 联合早报 | 新加坡中文新闻 |
| `cankaoxiaoxi` | 参考消息 | 国际新闻 |
| `sputniknewscn` | 卫星通讯社 | 俄罗斯中文新闻 |
| `kaopu` | 靠谱新闻 | 新闻聚合 |
| `tencent-hot` | 腾讯新闻热榜 | 热门新闻 |
| `chongbuluo-latest` | 虫部落最新 | 搜索聚合 |
| `chongbuluo-hot` | 虫部落热榜 | 搜索热榜 |

### 2.5 视频 / 娱乐（3个）

| 平台 ID | 名称 | 说明 |
|---|---|---|
| `qqvideo-tv-hotsearch` | 腾讯视频 | 电视剧热搜 |
| `iqiyi-hot-ranklist` | 爱奇艺 | 热度排行榜 |
| `steam` | Steam | 热门游戏/硬件 |

> **注意：** `nowcoder`、`freebuf`、`douban` 等也在此服务中，如需完整最新列表请调用批量接口传入空 `sources` 查看。

---

## 三、响应格式

### 3.1 单平台响应

```json
{
  "status": "cache",
  "id": "weibo",
  "updatedTime": 1780843079764,
  "items": [
    {
      "id": "搁浅央爆了",
      "title": "搁浅央爆了",
      "url": "https://s.weibo.com/weibo?q=%23...",
      "mobileUrl": "https://s.weibo.com/weibo?q=%23...",
      "extra": {
        "icon": {
          "url": "https://simg.s.weibo.com/moter/flags/1_0.png",
          "scale": 1.5
        }
      }
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `string` | `"cache"` 命中缓存，`"success"` 实时拉取 |
| `id` | `string` | 平台 ID |
| `updatedTime` | `number` | 数据更新时间（毫秒时间戳） |
| `items` | `array` | 热榜条目列表 |

**每条 item：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `string/number` | 条目唯一 ID（社交平台为标题，财经平台为数字ID） |
| `title` | `string` | 标题/内容 |
| `url` | `string` | PC 端链接 |
| `mobileUrl` | `string` | 移动端链接（可选） |
| `pubDate` | `number` | 发布时间（毫秒时间戳），**仅财经类平台有此字段** |
| `extra` | `object` | 扩展信息（如微博排名图标），可能为空 `{}` |

### 3.2 批量响应

```json
[
  {
    "status": "cache",
    "id": "weibo",
    "updatedTime": 1780843079764,
    "items": [...]
  },
  {
    "status": "success",
    "id": "cls-telegraph",
    "updatedTime": 1780843572325,
    "items": [...]
  }
]
```

批量接口返回一个数组，每个元素与单平台响应结构一致。

---

## 四、状态码与缓存

- HTTP 200：成功
- 不可用的平台会返回空 `items` 或报错

### 缓存策略

- 默认优先返回缓存（`"status": "cache"`），缓存时长因平台而异
- 需要实时数据时，单平台接口传 `?latest=true` 强制拉取最新
- 批量接口不支持 `latest` 参数，如需强制刷新请在批量拉取前逐个单平台调用 `?latest=true` 预热

---

## 五、与 stock_analysis_by_gpt 的集成场景

### 5.1 市场情绪信号

财经快讯（`cls-telegraph`、`wallstreetcn-quick`、`jin10`、`fastbull-express`）可以作为 `attention_signal` 的增量输入：

```python
import requests

def fetch_market_attention():
    """拉取财经快讯，统计术语频率生成情绪热力信号"""
    resp = requests.post(
        f"{BASE_URL}/api/s/entire",
        json={"sources": ["cls-telegraph", "wallstreetcn-quick", "jin10"]},
        headers=HEADERS,
    )
    sources = resp.json()
    titles = []
    for src in sources:
        for item in src.get("items", []):
            title = item.get("title", "")
            if title:
                titles.append(title)
    return titles  # 后续做关键词匹配/NLP情绪分析
```

### 5.2 主题热度验证

`derive-attention-signals` 现有逻辑可以从外部写入 `attention_signal` 表。热榜数据可直接用于：

- **主题热度确认**：某主题（如"AI大模型"）在各平台热榜出现的频率和排名
- **舆情突变检测**：某个公司/行业突然登上微博/知乎热搜，往往是重大事件的早期信号
- **雪球热门个股**：`xueqiu-hotstock` 直接告诉你当前散户最关注的股票

### 5.3 科技趋势发现

`github-trending-today`、`hackernews`、`producthunt`、`36kr-quick`、`aihot` 等科技源可以自动发现新兴技术主题，用于 `stock-intelligence-pipeline` 的主题发现环节（`_discover_pipeline_themes`）。

---

## 六、Python 客户端示例

```python
"""hotlist_client.py — 全网热榜聚合 API 客户端"""

import os
import requests
from typing import Optional

BASE_URL = os.getenv("HOTLIST_API_BASE_URL", "http://<HOST>:3220")
API_KEY = os.getenv("HOTLIST_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def get_hotlist(source_id: str, latest: bool = False, timeout: int = 10) -> dict:
    """获取单个平台热榜"""
    params = {"id": source_id}
    if latest:
        params["latest"] = "true"
    resp = requests.get(
        f"{BASE_URL}/api/s",
        params=params,
        headers=HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_hotlist_batch(sources: list[str], timeout: int = 30) -> list[dict]:
    """批量获取多平台热榜"""
    resp = requests.post(
        f"{BASE_URL}/api/s/entire",
        json={"sources": sources},
        headers=HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_all_sources(timeout: int = 60) -> list[dict]:
    """获取全部可用平台的热榜"""
    return get_hotlist_batch([], timeout=timeout)


def get_finance_headlines(timeout: int = 30) -> list[dict]:
    """只拉财经类快讯"""
    sources = [
        "cls-telegraph",
        "wallstreetcn-quick",
        "jin10",
        "fastbull-express",
        "gelonghui",
        "mktnews-flash",
    ]
    return get_hotlist_batch(sources, timeout=timeout)


def get_titles(source_id: str, latest: bool = False) -> list[str]:
    """只提取标题文本"""
    data = get_hotlist(source_id, latest=latest)
    return [item["title"] for item in data.get("items", []) if item.get("title")]


# --- CLI 自测 ---
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        source = sys.argv[1]
        data = get_hotlist(source, latest="--latest" in sys.argv)
        print(f"status={data['status']}  updated={data['updatedTime']}  items={len(data.get('items', []))}")
        for item in data.get("items", [])[:10]:
            pub = ""
            if item.get("pubDate"):
                from datetime import datetime
                pub = datetime.fromtimestamp(item["pubDate"] / 1000).strftime("%H:%M")
            print(f"  [{pub}] {item['title']}")
    else:
        # 默认拉财经快讯
        sources = get_finance_headlines()
        for src in sources:
            print(f"--- {src['id']} ({src['status']}) ---")
            for item in src.get("items", [])[:5]:
                print(f"  {item['title']}")
```

---

## 七、注意事项

1. **认证**：所有请求需携带 `X-API-Key` header。通过环境变量 `HOTLIST_API_KEY` 注入，切勿硬编码或提交到版本控制。
2. **服务可用性**：这是一个自部署服务，通过 `HOTLIST_API_BASE_URL` 访问（默认端口 3220），外网可达性取决于服务端网络策略。
3. **频率限制**：建议单平台拉取间隔 ≥1s，批量接口 ≥5s，避免对上游平台造成压力。
4. **缓存时长**：社交热榜通常缓存 5-15 分钟，财经快讯缓存 1-5 分钟。需要实时数据时传 `latest=true`。
5. **数据质量**：不同平台的 `title` 可能包含 HTML 实体或 Unicode 转义，建议用 Python 的 `html.unescape` 或保持 UTF-8 解码。
6. **财经快讯的 `pubDate`**：毫秒时间戳，非所有平台都提供。社交热榜通常没有发布时间。
7. **不可用平台**：部分平台可能因上游反爬或接口变更而暂时不可用，批量拉取时做好容错。

---

## 八、补充说明

- 该服务通过 `X-API-Key` 认证，请从服务维护方获取 key 后通过环境变量 `HOTLIST_API_KEY` 注入。
- 如需新增平台或反馈问题，联系服务维护方。
- 用于量化选股的财经快讯数据建议和 OHLCV 行情对齐时间窗口后使用，避免未来信息泄露。
