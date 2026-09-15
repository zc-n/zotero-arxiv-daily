"""
把每日推荐论文写进 Zotero 收藏夹的入口脚本（抗限流加固版）。

放置位置：src/zotero_arxiv_daily/main_zotero.py（与 main.py 同级）

它在原项目基础上多做了两件事：
  1. 把「从 arXiv 抓论文详情」这一步换成更耐用的版本：
     被 arXiv 限流时长时间重试；实在拿不到就跳过那一小批、继续往下走，
     绝不让整个任务崩掉（原版遇到 503 会直接报错退出，一封邮件都发不出）。
  2. 在发邮件之前，把推荐出来的论文额外写一份到指定的 Zotero 收藏夹。
"""

# 这行会最先输出。如果日志里连它都没有，说明文件内容不完整。
print("[ZOTERO-SYNC] 1/6 脚本开始运行", flush=True)

import os
import sys
import random
import logging
import traceback

os.environ["TOKENIZERS_PARALLELISM"] = "false"

import dotenv
import hydra
import requests
from loguru import logger
from omegaconf import DictConfig

dotenv.load_dotenv()

import zotero_arxiv_daily.executor as executor_module
from zotero_arxiv_daily.executor import Executor

try:
    from zotero_arxiv_daily.retriever.arxiv_retriever import ArxivRetriever
except Exception as _import_exc:  # 万一路径变了也不至于整个任务起不来
    ArxivRetriever = None
    print(f"[ZOTERO-SYNC] 提示：未能加载 arXiv 抓取模块（{_import_exc}），将沿用原版逻辑", flush=True)

print("[ZOTERO-SYNC] 2/6 依赖加载完成", flush=True)


def _say(message):
    """同时打到控制台和日志里，方便在 GitHub 日志里搜 ZOTERO-SYNC。"""
    print(f"[ZOTERO-SYNC] {message}", flush=True)
    logger.info(message)


def _env_int(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ============ 抗限流：替换 arXiv 抓取逻辑 ============

BATCH_SIZE = 50               # 一次向 arXiv 索取多少篇的详细信息（原版是 20）
MAX_TRIES = 6                 # 同一批最多尝试几次
INTER_BATCH_SLEEP = 5         # 每批之间歇多久（秒）
MAX_CONSECUTIVE_FAILURES = 5  # 连续这么多批都拿不到，就认定被临时封禁，收工
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_RETRIEVAL_MINUTES = 90    # 抓取阶段的硬性时间预算，超了就带着已有成果往下走


def _status_of(exc):
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _hardened_retrieve_raw_papers(self):
    """替换原版的 ArxivRetriever._retrieve_raw_papers。

    与原版的区别：
      * 一次要 50 篇而不是 20 篇，请求次数减少约 60%
      * 429 / 5xx 都会重试，退避时间更长（原版只认 429，遇到 503 直接崩）
      * 某一批最终拿不到时「跳过」而不是「抛错」，保证邮件仍能发出
      * 连续多批被拒时及时收手，不再硬撞 arXiv
    """
    import arxiv
    import feedparser
    from time import sleep
    from tqdm import tqdm

    query = '+'.join(self.config.source.arxiv.category)
    include_cross_list = self.config.source.arxiv.get("include_cross_list", False)

    _say(f"读取 arXiv 每日列表（分类：{query}）")
    feed = feedparser.parse(f"https://rss.arxiv.org/atom/{query}")
    feed_title = getattr(feed.feed, "title", "") or ""
    if "Feed error for query" in feed_title:
        raise Exception(f"Invalid ARXIV_QUERY: {query}.")

    allowed = {"new", "cross"} if include_cross_list else {"new"}
    all_ids = [
        entry.id.removeprefix("oai:arXiv.org:")
        for entry in feed.entries
        if entry.get("arxiv_announce_type", "new") in allowed
    ]
    if self.config.executor.debug:
        all_ids = all_ids[:10]

    total_found = len(all_ids)
    if total_found == 0:
        _say("arXiv 今天没有新论文（周末和节假日通常如此）")
        return []
    _say(f"arXiv 今日新增 {total_found} 篇候选论文")

    # 可选：限制候选池大小。arXiv 对云服务器的 IP 限流很凶，
    # 候选越少、请求越少，被拦住的概率越低。
    cap = _env_int("ARXIV_MAX_CANDIDATES", 200)
    if cap and len(all_ids) > cap:
        all_ids = random.sample(all_ids, cap)
        _say(
            f"为降低被限流的概率，从 {total_found} 篇中随机抽取 {cap} 篇进入候选池"
            f"（想调整可改环境变量 ARXIV_MAX_CANDIDATES，设为 0 表示不限制）"
        )

    total = len(all_ids)
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    _say(f"开始逐批获取论文详情，共 {batches} 批")

    client = arxiv.Client(num_retries=1, delay_seconds=3)
    raw_papers = []
    bar = tqdm(total=total)
    consecutive_failures = 0
    failed_batches = 0

    from time import monotonic
    budget_seconds = max(_env_int("ARXIV_MAX_MINUTES", MAX_RETRIEVAL_MINUTES), 1) * 60
    started_at = monotonic()

    for batch_no, start in enumerate(range(0, total, BATCH_SIZE), 1):
        if monotonic() - started_at > budget_seconds:
            _say(
                f"抓取已耗时超过 {budget_seconds // 60} 分钟，为避免整个任务超时被强制中断，"
                f"就此结束抓取、带着已拿到的 {len(raw_papers)} 篇继续往下走"
            )
            break
        ids = all_ids[start:start + BATCH_SIZE]
        search = arxiv.Search(id_list=ids)
        got = False
        for attempt in range(MAX_TRIES):
            try:
                batch = list(client.results(search))
                bar.update(len(batch))
                raw_papers.extend(batch)
                got = True
                break
            except Exception as exc:
                status = _status_of(exc)
                detail = type(exc).__name__ + ("" if status is None else f" {status}")
                retryable = status is None or status in RETRYABLE_STATUS
                if retryable and attempt < MAX_TRIES - 1:
                    wait = min(30 * (attempt + 1), 180)
                    _say(
                        f"第 {batch_no}/{batches} 批被 arXiv 限流（{detail}），"
                        f"{wait} 秒后重试（第 {attempt + 2}/{MAX_TRIES} 次）"
                    )
                    sleep(wait)
                else:
                    _say(
                        f"第 {batch_no}/{batches} 批最终失败（{detail}），"
                        f"跳过这 {len(ids)} 篇，继续后面的批次"
                    )
                    break

        if got:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            failed_batches += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                _say(
                    f"arXiv 连续 {consecutive_failures} 批全部被拒，判定为临时封禁，"
                    f"提前结束本次抓取（不再硬撞，以免封禁时间变长）"
                )
                break

        if start + BATCH_SIZE < total:
            sleep(INTER_BATCH_SLEEP)

    bar.close()

    if failed_batches:
        _say(f"本次有 {failed_batches} 批因限流被跳过，最终拿到 {len(raw_papers)} 篇候选论文")
    else:
        _say(f"arXiv 论文抓取完成，共 {len(raw_papers)} 篇")
    return raw_papers


# 装载补丁：原版这个方法遇到 503 会直接抛错让整个任务失败
if ArxivRetriever is not None:
    ArxivRetriever._retrieve_raw_papers = _hardened_retrieve_raw_papers
    print("[ZOTERO-SYNC] 3/6 抗限流补丁已装载", flush=True)
else:
    print("[ZOTERO-SYNC] 3/6 抗限流补丁未装载（沿用原版逻辑）", flush=True)


# ============ 写回 Zotero ============

def _creators(authors):
    """把 ["Yann LeCun", "John Smith"] 转成 Zotero 的作者格式。"""
    out = []
    for raw in authors or []:
        parts = str(raw or "").strip().split()
        if not parts:
            continue
        if len(parts) == 1:
            out.append({"creatorType": "author", "name": parts[0]})
        else:
            out.append({
                "creatorType": "author",
                "firstName": " ".join(parts[:-1]),
                "lastName": parts[-1],
            })
    return out


def _archive_id(url):
    """从 https://arxiv.org/abs/2501.12345 取出 arXiv:2501.12345，用于去重。"""
    url = url or ""
    if "arxiv.org/abs/" in url:
        return "arXiv:" + url.split("/abs/", 1)[1].strip("/")
    return ""


def _to_item(paper, collection):
    """把一篇推荐论文转成 Zotero 条目。"""
    title = getattr(paper, "title", "") or ""
    abstract = getattr(paper, "abstract", "") or ""
    url = getattr(paper, "url", "") or ""
    authors = getattr(paper, "authors", None) or []
    score = getattr(paper, "score", None)
    tldr = getattr(paper, "tldr", None)
    affiliations = getattr(paper, "affiliations", None) or []
    pdf_url = getattr(paper, "pdf_url", None)
    source = getattr(paper, "source", None) or "arxiv"

    extra = [f"Source: {source}"]
    if isinstance(score, (int, float)):
        extra.append(f"Relevance score: {score:.4f}")
    if tldr:
        extra.append(f"TLDR: {tldr}")
    if affiliations:
        extra.append("Affiliations: " + "; ".join(str(a) for a in affiliations))
    if pdf_url:
        extra.append(f"PDF: {pdf_url}")

    item = {
        "itemType": "preprint",
        "title": title,
        "creators": _creators(authors),
        "abstractNote": abstract,
        "url": url,
        "repository": "arXiv" if source == "arxiv" else source,
        "extra": "\n".join(extra),
        "collections": [collection],
        "tags": [{"tag": "auto-recommended"}],
    }
    aid = _archive_id(url)
    if aid:
        item["archiveID"] = aid
    return item


def sync_to_zotero(papers, user_id, api_key):
    """把推荐论文写入 Zotero。任何错误都只记日志，绝不影响邮件发送。"""
    collection = (os.environ.get("ZOTERO_COLLECTION_KEY") or "").strip()
    if not collection:
        _say("未设置 ZOTERO_COLLECTION_KEY，跳过写入 Zotero")
        return
    if not papers:
        _say("本次没有推荐论文，跳过写入 Zotero")
        return

    root = f"https://api.zotero.org/users/{user_id}"
    headers = {"Zotero-API-Key": str(api_key), "Content-Type": "application/json"}

    # 先读出收藏夹里已有的 arXiv 编号，避免重复添加
    known, offset = set(), 0
    try:
        while True:
            resp = requests.get(
                f"{root}/collections/{collection}/items/top",
                headers=headers,
                params={"limit": 100, "start": offset},
                timeout=60,
            )
            if resp.status_code != 200:
                _say(f"读取收藏夹失败 [{resp.status_code}]，本次不做去重：{resp.text[:200]}")
                break
            batch = resp.json()
            if not batch:
                break
            for entry in batch:
                data = entry.get("data", {}) or {}
                aid = data.get("archiveID") or _archive_id(data.get("url", ""))
                if aid:
                    known.add(aid)
            offset += len(batch)
            if len(batch) < 100:
                break
    except Exception as exc:
        _say(f"读取收藏夹出错，本次不做去重：{exc}")

    items = []
    for paper in papers:
        item = _to_item(paper, collection)
        aid = item.get("archiveID")
        if aid and aid in known:
            continue
        items.append(item)

    if not items:
        _say(f"推荐论文已全部存在于收藏夹 {collection}，无需写入")
        return

    # Zotero 单次最多接受 50 条，超出需要分批
    written = 0
    for start in range(0, len(items), 50):
        chunk = items[start:start + 50]
        resp = requests.post(f"{root}/items", headers=headers, json=chunk, timeout=180)
        if resp.status_code not in (200, 201) and "archiveID" in resp.text:
            # 个别 Zotero 版本不认 archiveID 字段，去掉后重试一次
            for it in chunk:
                it.pop("archiveID", None)
            resp = requests.post(f"{root}/items", headers=headers, json=chunk, timeout=180)
        if resp.status_code in (200, 201):
            written += len(chunk)
        else:
            _say(f"写入 Zotero 失败 [{resp.status_code}] {resp.text[:400]}")

    _say(f"已写入 {written} 篇论文到 Zotero 收藏夹 {collection}")


@hydra.main(version_base=None, config_path="../../config", config_name="default")
def main(config: DictConfig):
    logger.remove()
    logger.add(
        sys.stdout,
        level="DEBUG" if config.executor.debug else "INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )
    for name in logging.root.manager.loggerDict:
        if "zotero_arxiv_daily" not in name:
            logging.getLogger(name).setLevel(logging.WARNING)

    # Executor.run() 内部会调用 render_email(推荐论文列表)。
    # 在这里把它换成「先写 Zotero，再照常渲染邮件」，就能白拿到推荐结果。
    original_render_email = executor_module.render_email

    def render_email_and_sync(papers):
        try:
            sync_to_zotero(papers, config.zotero.user_id, config.zotero.api_key)
        except Exception as exc:
            _say(f"写入 Zotero 出错，已跳过：{exc}")
            traceback.print_exc()
        return original_render_email(papers)

    executor_module.render_email = render_email_and_sync

    print("[ZOTERO-SYNC] 5/6 开始抓取与排序（这一步最慢，属正常）", flush=True)
    Executor(config).run()
    print("[ZOTERO-SYNC] 6/6 主流程结束", flush=True)


# ↓↓↓ 文件必须以此结尾，缺了它 Python 会什么都不做、也不报错 ↓↓↓
if __name__ == "__main__":
    print("[ZOTERO-SYNC] 4/6 即将调用主流程（看到这行说明文件结尾是完整的）", flush=True)
    try:
        main()
    except Exception:
        print("[ZOTERO-SYNC] 主流程异常退出，错误信息如下：", flush=True)
        traceback.print_exc()
        sys.exit(1)
# ==== 文件到此结束。能看到这一行，说明内容一直到结尾都在 ====
