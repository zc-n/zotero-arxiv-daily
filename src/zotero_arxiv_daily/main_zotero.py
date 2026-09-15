"""
把每日推荐论文写进 Zotero 收藏夹的入口脚本。

放置位置：src/zotero_arxiv_daily/main_zotero.py（与 main.py 同级）
作用：完整复用原有的抓取 / 排序 / 写邮件逻辑，只在发邮件之前，
      把推荐出来的论文额外写一份到你在 Zotero 里指定的收藏夹。
"""
import os
import sys
import logging

import dotenv
import hydra
import requests
from loguru import logger
from omegaconf import DictConfig

os.environ["TOKENIZERS_PARALLELISM"] = "false"
dotenv.load_dotenv()

import zotero_arxiv_daily.executor as executor_module
from zotero_arxiv_daily.executor import Executor


def _creators(authors):
    """把 ["Yann LeCun", "John Smith"] 转成 Zotero 的作者格式。"""
    out = []
    for raw in authors or []:
        parts = (raw or "").strip().split()
        if len(parts) == 1:
            out.append({"creatorType": "author", "name": parts[0]})
        elif len(parts) > 1:
            out.append({
                "creatorType": "author",
                "firstName": " ".join(parts[:-1]),
                "lastName": parts[-1],
            })
    return out


def _archive_id(url):
    """从 https://arxiv.org/abs/2501.12345 取出 arXiv:2501.12345，用于去重。"""
    if url and "arxiv.org/abs/" in url:
        return "arXiv:" + url.split("/abs/", 1)[1].strip("/")
    return ""


def _to_item(paper, collection):
    """把一篇推荐论文转成 Zotero 条目。"""
    extra = [f"Source: {paper.source}"]
    if paper.score is not None:
        extra.append(f"Relevance score: {paper.score:.4f}")
    if paper.tldr:
        extra.append(f"TLDR: {paper.tldr}")
    if paper.affiliations:
        extra.append("Affiliations: " + "; ".join(paper.affiliations))
    if paper.pdf_url:
        extra.append(f"PDF: {paper.pdf_url}")

    item = {
        "itemType": "preprint",
        "title": paper.title or "",
        "creators": _creators(paper.authors),
        "abstractNote": paper.abstract or "",
        "url": paper.url or "",
        "repository": "arXiv" if paper.source == "arxiv" else paper.source,
        "extra": "\n".join(extra),
        "collections": [collection],
        "tags": [{"tag": "auto-recommended"}],
    }
    aid = _archive_id(paper.url)
    if aid:
        item["archiveID"] = aid
    return item


def sync_to_zotero(papers, user_id, api_key):
    """把推荐论文写入 Zotero。任何错误都只记日志，绝不影响邮件发送。"""
    collection = (os.environ.get("ZOTERO_COLLECTION_KEY") or "").strip()
    if not collection:
        logger.warning("未设置 ZOTERO_COLLECTION_KEY，跳过写入 Zotero")
        return
    if not papers:
        logger.info("本次没有推荐论文，跳过写入 Zotero")
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
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for entry in batch:
                aid = entry.get("data", {}).get("archiveID")
                if aid:
                    known.add(aid)
            offset += len(batch)
            if len(batch) < 100:
                break
    except Exception as exc:
        logger.warning(f"读取收藏夹失败，本次不做去重：{exc}")

    items = [
        item for item in (_to_item(p, collection) for p in papers)
        if not (item.get("archiveID") and item["archiveID"] in known)
    ]
    if not items:
        logger.info("推荐论文已全部存在于该收藏夹，无需写入")
        return

    # Zotero 单次最多接受 50 条，超出需要分批
    written = 0
    for offset in range(0, len(items), 50):
        chunk = items[offset:offset + 50]
        resp = requests.post(f"{root}/items", headers=headers, json=chunk, timeout=180)
        if resp.status_code in (200, 201):
            written += len(chunk)
        else:
            logger.error(f"写入 Zotero 失败 [{resp.status_code}] {resp.text[:300]}")
    logger.info(f"已写入 {written} 篇论文到 Zotero 收藏夹 {collection}")


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
            logger.exception(f"写入 Zotero 出错，已跳过：{exc}")
        return original_render_email(papers)

    executor_module.render_email = render_email_and_sync
    Executor(config).run()


if __name__ == "__&#8203;main__":
    main()
