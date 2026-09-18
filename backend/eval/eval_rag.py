"""RAG 召回率离线评测脚本

用法:
    cd backend
    venv/Scripts/python.exe eval/eval_rag.py

依赖:
    - 已配置 DASHSCOPE_API_KEY (脚本会提示)
    - 知识库已索引 (脚本会自动 ensure, 空库时才重建)

指标:
    Recall@k = 命中的相关片段数 / 标注的相关片段总数
    MRR      = 第一个相关片段排名的倒数 (所有 query 平均)

评测集格式 (eval_set.json): 每条 = query + city + relevant(标注的相关片段)
    relevant 用「来源文件名 + 必含关键词」来定位片段, 命中判定为:
    检索结果的 metadata.source 命中 source 且 page_content 包含 contains。
"""

import json
import sys
from pathlib import Path

# 让脚本能 import app 包 (与 tests/conftest.py 同款做法)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK, 打印中文前先切 UTF-8, 避免 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.services.rag_service import get_rag_service

EVAL_SET_PATH = Path(__file__).resolve().parent / "eval_set.json"
K = 3  # 评测 top-k


def load_eval_set() -> list:
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


def is_hit(doc, rel: dict) -> bool:
    """判断一个检索结果 Document 是否命中标注的相关片段"""
    return doc.metadata.get("source") == rel.get("source") and rel.get("contains", "") in doc.page_content


def evaluate(rag) -> None:
    eval_set = load_eval_set()
    total_recall = 0.0
    total_mrr = 0.0

    print(f"知识库评测集: {len(eval_set)} 条, top-k = {K}\n")

    for i, item in enumerate(eval_set, 1):
        query = item["query"]
        city = item.get("city")
        relevant = item.get("relevant", [])

        docs = rag.retrieve_documents(query, city=city, k=K)

        hit = 0
        first_rank = 0  # 第一个命中的相关片段在结果中的排名 (1 起)
        for rel in relevant:
            for idx, doc in enumerate(docs):
                if is_hit(doc, rel):
                    hit += 1
                    if first_rank == 0 or idx + 1 < first_rank:
                        first_rank = idx + 1
                    break

        recall = hit / len(relevant) if relevant else 0.0
        mrr = 1.0 / first_rank if first_rank else 0.0
        total_recall += recall
        total_mrr += mrr

        print(f"[{i}] query={query!r}  city={city}")
        print(f"    Recall@{K} = {recall:.2f}   MRR = {mrr:.2f}")
        for doc in docs:
            src = doc.metadata.get("source") or f"record_id={doc.metadata.get('record_id')}"
            print(f"      - {src}: {doc.page_content[:40]}...")
        print()

    n = len(eval_set)
    print("=" * 60)
    print(f"平均 Recall@{K}: {total_recall / n:.2f}")
    print(f"平均 MRR:        {total_mrr / n:.2f}")
    print("=" * 60)


if __name__ == "__main__":
    rag = get_rag_service()
    if not rag.enabled:
        print("❌ RAG 未启用: 请先配置 DASHSCOPE_API_KEY")
        sys.exit(1)
    rag.ensure_knowledge_index()
    evaluate(rag)
