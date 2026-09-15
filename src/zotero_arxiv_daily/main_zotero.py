"""
把每日推荐论文写进 Zotero 收藏夹的入口脚本（自带进度提示版）。

放置位置：src/zotero_arxiv_daily/main_zotero.py（与 main.py 同级）
粘贴要求：必须从第一行一直到最后一行的「文件到此结束」都贴上

作用：完整复用原有的抓取 / 排序 / 写邮件逻辑，只在发邮件之前，
      把推荐出来的论文额外写一份到你在 Zotero 里指定的收藏夹。
"""

# 这行会最先输出。如果日志里连它都没有，说明文件内容不完整。
print("[ZOTERO-SYNC] 1/5 脚本开始运行", flush=True)

import os
import sys
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

print("[ZOTERO-SYNC] 2/5 依赖加载完成", flush=True)


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


def _say(message):
    """同时打到控制台和日志里，方便在 GitHub 日志里搜 ZOTERO-SYNC。"""
    print(f"[ZOTERO-SYNC] {message}", flush=True)
    logger.info(message)


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

    print("[ZOTERO-SYNC] 4/5 开始抓取与排序（这一步最慢，属正常）", flush=True)
    Executor(config).run()
    print("[ZOTERO-SYNC] 5/5 主流程结束", flush=True)


# ↓↓↓ 文件必须以此结尾，缺了它 Python 会什么都不做、也不报错 ↓↓↓
if __name__ == "__main__":
    print("[ZOTERO-SYNC] 3/5 即将调用主流程（看到这行说明文件结尾是完整的）", flush=True)
    try:
        main()
    except Exception:
        print("[ZOTERO-SYNC] 主流程异常退出，错误信息如下：", flush=True)
        traceback.print_exc()
        sys.exit(1)
# ==== 文件到此结束。能看到这一行，说明内容一直到结尾都在 ====
