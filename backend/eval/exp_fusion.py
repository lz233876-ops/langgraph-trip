"""向量 + BM25 融合参数扫描 (一次性脚本)

缘起: 给 retrieve_documents 加了 BM25 双路召回后, 难组 MRR 0.86 -> 0.90, 但失败
案例只是换了一条 —— 说明 RRF 的默认常数(_RRF_K=60)在这个规模下不对。标准 K=60 是
给"大语料 + 深候选池"用的; 本语料每城只有 15 个片段、每路只取 3 条, 1/(60+rank)
把排名差距压得太平, 于是"两路都弱命中"的块可以压过"单路排第 1"的块。

本脚本固定两路候选(向量取 top-10 后按深度切片, 所以只需查一次), 离线扫
RRF_K × 候选深度的组合, 找出最优参数。零生产改动。

用法:
    cd backend
    venv/Scripts/python.exe eval/exp_fusion.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.services.rag_service import get_rag_service
from eval_rag import K, load_eval_set, matches

MAX_DEPTH = 10          # 两路都取这么深, 再切片成不同 depth
DEPTHS = [3, 5, 10]
RRF_KS = [1, 5, 10, 60]


def fuse(vector_docs: list, keyword_docs: list, rrf_k: int, depth: int) -> list:
    """RRF 融合 (与 rag_service._fused_search 同逻辑, 参数可调)"""
    scores: dict = {}
    by_key: dict = {}
    for docs in (vector_docs[:depth], keyword_docs[:depth]):
        for rank, doc in enumerate(docs):
            key = doc.page_content
            by_key[key] = doc
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
    return [by_key[k_] for k_ in sorted(scores, key=lambda d: -scores[d])[:K]]


def score_ranked(top: list, item: dict) -> tuple:
    rel = item["relevant"]
    first_rank = hit = 0
    for r in rel:
        rank = next((n for n, d in enumerate(top, 1) if matches(d, r)), 0)
        if rank:
            hit += 1
            if first_rank == 0 or rank < first_rank:
                first_rank = rank
    return hit / len(rel), (1.0 / first_rank if first_rank else 0.0)


def main() -> None:
    rag = get_rag_service()
    if not rag.enabled:
        print("❌ RAG 未启用: 请先配置 DASHSCOPE_API_KEY")
        sys.exit(1)

    items = [i for i in load_eval_set() if i.get("group") in ("easy", "hard")]
    print(f"用例 {len(items)} 条 (easy+hard) | top-k = {K} | 两路候选深度 {MAX_DEPTH}\n")

    # 两路候选各查一次, 后续所有参数组合都在内存里切片重排
    print("取两路候选 (向量需调用 embedding)...")
    pairs = []
    for item in items:
        vec = rag._knowledge_store.similarity_search(
            item["query"], k=MAX_DEPTH, filter={"city": item["city"]}
        )
        kw = rag._keyword_search(item["query"], item["city"], MAX_DEPTH)
        pairs.append((item, vec, kw))

    def evaluate(get_top) -> tuple:
        recall = mrr = 0.0
        missed = []
        for item, vec, kw in pairs:
            r, m = score_ranked(get_top(item, vec, kw), item)
            recall += r
            mrr += m
            if r == 0:
                missed.append(item["query"])
        n = len(pairs)
        return recall / n, mrr / n, missed

    print("\n" + "=" * 72)
    print("参照组 (单路)")
    print("=" * 72)
    for name, get_top in [
        ("仅向量", lambda i, v, k: v[:K]),
        ("仅 BM25", lambda i, v, k: k[:K]),
    ]:
        recall, mrr, missed = evaluate(get_top)
        print(f"{name:<14} Recall@3={recall:.2f}  MRR={mrr:.2f}  未召回 {len(missed)}")

    print("\n" + "=" * 72)
    print("RRF 融合 (行=RRF_K, 列=候选深度)")
    print("=" * 72)
    print(f"{'RRF_K':<8}" + "".join(f"{f'depth={d}':>18}" for d in DEPTHS))
    best = None
    for rrf_k in RRF_KS:
        cells = []
        for depth in DEPTHS:
            recall, mrr, missed = evaluate(
                lambda i, v, k, rk=rrf_k, dp=depth: fuse(v, k, rk, dp)
            )
            cells.append(f"{recall:.2f}/{mrr:.2f}({len(missed)})")
            cand = (mrr, recall, rrf_k, depth, missed)
            if best is None or cand > best:
                best = cand
        print(f"{rrf_k:<8}" + "".join(f"{c:>18}" for c in cells))
    print("\n单元格格式: Recall/MRR(未召回数)")

    mrr, recall, rrf_k, depth = best[0], best[1], best[2], best[3]
    print("\n" + "=" * 72)
    print(f"最优: RRF_K={rrf_k}  候选深度={depth}  ->  Recall@3={recall:.2f}  MRR={mrr:.2f}")
    if best[4]:
        print(f"  仍未召回: {', '.join(best[4])}")
    if (rrf_k, depth) != (60, K):
        print(f"  当前生产用的是 RRF_K=60 / 深度={K}, 建议改为上面这组。")


if __name__ == "__main__":
    main()
