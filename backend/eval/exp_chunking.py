"""切块 / 嵌入文本变体对比实验 (一次性脚本)

缘起: 跑 eval_rag.py 发现 46 个知识块共用同一套模板正文 (- 门票：/- 开放时间：
/- 交通：...), 块间余弦中位 0.647, 而 query 对各块余弦只有 0.44~0.50, 排序近乎
噪声 —— 「故宫门票多少钱」召回不到故宫。

本脚本离线对比几种「嵌入文本」构造方式。不碰生产代码、不重建生产索引, 只在内存
里重建向量并排序, 所以可以随便试。选定赢家后再去改 rag_service。

用法:
    cd backend
    venv/Scripts/python.exe eval/exp_chunking.py

变体:
    V0 旧切块      = 改造前的生产行为 (RecursiveCharacterTextSplitter 300/50), 基线
    V1 单段+标题   = 按 ## / ### 切成一段一块, 嵌入文本前置 "城市 + 标题"  ← 已采纳
    V2 去模板标签  = 同 V1, 再把跨块重复的 "- 门票：" 一类标签去掉
    V3 仅标题     = 同 V1, 只留 "城市 + ### 标题", 探上限 (正文是不是纯噪声)

2026-09 实测结论: V1 被采纳 (生产合计 Recall 0.96 / MRR 0.91); V2 更高 (1.00/0.97)
但要去掉存储文本里的标签, 折损注入 prompt 的可读性; V3 反而比 V1 差, 说明正文细节
是有用的, 不能只留标题。

自检: V1 就是当前生产, 应复现 eval_rag.py 的合计数字 (Recall 0.96 / MRR 0.91),
否则说明本脚本的离线排序和生产不一致, 其他变体的结论也不能信。
"""

import json
import re
import statistics
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.services.rag_service import (
    KNOWLEDGE_DIR,
    _CITY_NAME_MAP,
    get_rag_service,
)
from eval_rag import EVAL_SET_PATH, _HEADER_RE, K, load_eval_set, matches

# 跨块重复的模板标签 —— 46 个块每个都出现一次, 对区分景点零贡献
_LABEL_RE = re.compile(r"^-\s*(?:门票|开放时间|建议游玩|地址|交通|打卡点|避坑)[:：]\s*")

# 与 rag_service._text_splitter 完全一致, 保证 V0 就是生产行为
_BASELINE_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=300,
    chunk_overlap=50,
    separators=["\n## ", "\n### ", "\n- ", "\n", "。", "；", " "],
)


def parse_sections(text: str) -> list:
    """把 md 拆成 [(h2, h3, 正文行)] —— 一段一块, 不跨节合并

    纯容器型 `## 标题` (无直属正文, 内容全在 ### 子节里, 如「必去景点」)会被跳过,
    避免产生空块。生产现状里这种容器块会跟第一个景点粘在一起, 混进无关标题。
    """
    out = []
    h2 = h3 = None
    buf: list = []

    def flush():
        if h2 and (buf or h3):
            out.append((h2, h3, list(buf)))

    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("### "):
            flush()
            h3, buf = s[4:].strip(), []
        elif s.startswith("## "):
            flush()
            h2, h3, buf = s[3:].strip(), None, []
        elif s.startswith("# "):
            continue
        elif s:
            buf.append(s)
    flush()
    return out


def build_docs(variant: str, city: str, text: str) -> list:
    """构造某个变体下的 Document 列表 (嵌入文本即 page_content)"""
    if variant == "V0":
        return [
            Document(page_content=c, metadata={"city": city})
            for c in _BASELINE_SPLITTER.split_text(text)
        ]

    docs = []
    for h2, h3, lines in parse_sections(text):
        title = h3 or h2
        header = f"### {h3}" if h3 else f"## {h2}"
        if variant == "V3":  # 仅标题, 探上限
            page = f"{city} {title}\n{header}"
        else:
            body = "\n".join(lines)
            if variant == "V2":  # 去掉重复模板标签
                body = "\n".join(_LABEL_RE.sub("", line) for line in lines)
            page = f"{city} {title}\n{header}\n{body}"
        docs.append(Document(page_content=page, metadata={"city": city, "title": title}))
    return docs


def dot(a, b) -> float:
    """单位向量点积 == 余弦 (嵌入已归一化, 范数 1.0)"""
    return sum(x * y for x, y in zip(a, b))


def doc_doc_median(vecs: list) -> float:
    """块间余弦中位数 —— 越低说明块之间越分得开"""
    if len(vecs) < 2:
        return float("nan")
    return statistics.median(dot(vecs[i], vecs[j]) for i, j in combinations(range(len(vecs)), 2))


def score(qvecs: list, docs: list, docvecs: list, items: list) -> tuple:
    """按城市过滤 + 余弦排序取 top-k, 返回 (平均Recall, 平均MRR, 未召回query列表)"""
    recall_sum = mrr_sum = 0.0
    missed = []
    for item, qv in zip(items, qvecs):
        cand = [i for i, d in enumerate(docs) if d.metadata["city"] == item["city"]]
        top = sorted(cand, key=lambda i: -dot(qv, docvecs[i]))[:K]

        first_rank = hit = 0
        for rel in item["relevant"]:
            rank = next((n for n, i in enumerate(top, 1) if matches(docs[i], rel)), 0)
            if rank:
                hit += 1
                if first_rank == 0 or rank < first_rank:
                    first_rank = rank
        recall_sum += hit / len(item["relevant"])
        mrr_sum += 1.0 / first_rank if first_rank else 0.0
        if not first_rank:
            missed.append(item["query"])
    n = len(items)
    return recall_sum / n, mrr_sum / n, missed


def main() -> None:
    rag = get_rag_service()
    if not rag.enabled:
        print("❌ RAG 未启用: 请先配置 DASHSCOPE_API_KEY")
        sys.exit(1)

    items = [i for i in load_eval_set() if i.get("group") in ("easy", "hard")]
    print(f"用例 {len(items)} 条 (easy+hard, 不含负样本) | top-k = {K}")
    print("嵌入模型:", rag._embedding.model, "\n")

    # 查询向量只算一次, 四个变体共用 (text_type 参数已证实无效果, 不影响结果)
    print("嵌入查询...")
    qvecs = rag._embedding.embed_documents([i["query"] for i in items])

    # 各城市原文
    raw = {
        _CITY_NAME_MAP.get(p.stem, p.stem): p.read_text(encoding="utf-8")
        for p in sorted(KNOWLEDGE_DIR.glob("*.md"))
    }

    variants = [
        ("V0", "旧切块 (基线)"),
        ("V1", "单段+标题 (生产)"),
        ("V2", "去模板标签"),
        ("V3", "仅标题"),
    ]

    results = {}
    for code, label in variants:
        docs = []
        for city, text in raw.items():
            docs.extend(build_docs(code, city, text))
        print(f"嵌入 {code} ({len(docs)} 块)...")
        docvecs = rag._embedding.embed_documents([d.page_content for d in docs])
        recall, mrr, missed = score(qvecs, docs, docvecs, items)
        results[code] = (label, len(docs), recall, mrr, missed, doc_doc_median(docvecs))

    print("\n" + "=" * 78)
    print(f"{'变体':<16}{'块数':>5}{'Recall@3':>11}{'MRR':>8}{'块间余弦中位':>14}  未召回")
    print("=" * 78)
    for code, label in variants:
        _, n_docs, recall, mrr, missed, dd = results[code]
        print(f"{code} {label:<12}{n_docs:>5}{recall:>11.2f}{mrr:>8.2f}{dd:>14.3f}  {len(missed)}")
    print("=" * 78)

    print("\n各变体未召回的 query (Recall 掉到 0 的):")
    for code, label in variants:
        missed = results[code][4]
        print(f"  {code} {label:<12}: {', '.join(missed) if missed else '(无)'}")

    base_recall, base_mrr = results["V0"][2], results["V0"][3]
    prod_recall, prod_mrr = results["V1"][2], results["V1"][3]
    print(f"\n自检: V1 即当前生产, 应复现 eval_rag.py 的合计 Recall 0.96 / MRR 0.91 -> "
          f"实测 {prod_recall:.2f} / {prod_mrr:.2f}")

    best = max(variants, key=lambda v: results[v[0]][3])[0]
    if best == "V0":
        print("\n结论: 没有变体超过现状, 换嵌入文本解决不了。该考虑混合检索 (BM25) 了。")
        return

    print(f"\n最佳变体: {best} {results[best][0]}")
    print(f"  MRR {base_mrr:.2f} -> {results[best][3]:.2f}")
    base_missed = set(results["V0"][4])
    best_missed = set(results[best][4])
    fixed = base_missed - best_missed
    broke = best_missed - base_missed
    if fixed:
        print(f"  修好的 query ({len(fixed)}): {', '.join(sorted(fixed))}")
    if broke:
        print(f"  变差/仍失败的 query ({len(broke)}): {', '.join(sorted(broke))}")


if __name__ == "__main__":
    main()
