"""RAG 知识库检索离线评测脚本

用法:
    cd backend
    venv/Scripts/python.exe eval/eval_rag.py

依赖:
    - 已配置 DASHSCOPE_API_KEY (未配置时脚本直接退出)
    - 知识库已索引 (脚本会自动 ensure, 仅在空库时重建)

为什么这样设计 (小语料专用):
    知识库目前只有 4 个文件 / 约 60 个片段, top-k=3 一次就抽走单城约 20% 的语料,
    纯召回率必然趋近 1.0, 跑出来的 1.00 没有信息量。所以分成三组各有分工:
      easy     — 含景点名的直接查询。预期恒为全中, 当回归基线:
                 一旦掉下来说明索引/切块/过滤坏了, 而不是"检索变差了"。
      hard     — 不含景点名的意图查询。主力信号, 看 MRR:
                 正确片段是排第 1 还是被挤到第 3。
      negative — 城市过滤守卫。用「明显属于 A 城的查询 + B 城的过滤条件」,
                 断言结果不出现 A 城片段; 并反向验证不过滤时确实会串城,
                 否则这条负样本是空转的假信号。

标注格式: relevant 填「章节标题名」而非文件名+子串。标题是生产代码已依赖的
稳定结构 (本地景点详情解析 rag_service._parse_attraction_details 按 ### 切段),
所以改正文措辞不会连累评测集; 只有改标题时才需要重新标注。

评测范围: 只评测知识库片段 (metadata 含 source), 刻意剔除历史行程
(metadata 含 record_id)。历史行程按城市累积、无标注、每次生成行程都会变化,
混进来会让评测不可复现。知识库与历史行程的 k 是分别取的, 事后剔除不会
影响知识库的 top-k 配额。
"""

import json
import re
import sys
from pathlib import Path

# 让脚本能 import app 包 (与 tests/conftest.py 同款做法)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK, 打印中文前先切 UTF-8, 避免 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.services.rag_service import RagService, get_rag_service

EVAL_SET_PATH = Path(__file__).resolve().parent / "eval_set.json"
K = 3  # 评测 top-k

# 章节标题行: ## 或 ### 开头 (与知识库文档的层级约定一致)
_HEADER_RE = re.compile(r"^#{2,3}\s+(.+)$", re.M)

GROUPS = [
    ("easy", "简单组 | 含景点名的直接查询 —— 回归基线, 预期全中"),
    ("hard", "难组   | 不含景点名的意图查询 —— 主力信号, 看 MRR"),
    ("negative", "负样本 | 城市过滤守卫 —— 断言不串城"),
]


def load_eval_set() -> list:
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


def knowledge_docs(rag, query: str, city: str) -> list:
    """取检索结果中的知识库片段 (剔除历史行程)"""
    docs = rag.retrieve_documents(query, city=city, k=K)
    return [d for d in docs if "source" in d.metadata]


def named_sections(doc) -> str:
    """片段包含的章节标题 (用 + 连接)

    一个片段可能含多个章节 —— 切块是「按标题切 + 不超 chunk_size 就合并」,
    例如「北京路步行街」+「陈家祠」共约 290 字符会被并成一块。直接列出全部
    标题, 才能看清命中凭的是哪一段, 而不是被首行 32 字符误导。
    """
    headers = [h.strip() for h in _HEADER_RE.findall(doc.page_content)]
    return " + ".join(headers) if headers else "(无标题)"


def matches(doc, section: str) -> bool:
    """片段是否命中标注的章节

    只看片段里的标题行, 不看正文 —— 正文提到某景点不代表这块讲的它
    (例如「经典路线」片段正文会罗列一堆景点名, 不该算命中那些景点)。
    标题名匹配复用 RagService._normalize_name, 与生产代码同一套规则:
    去掉括号内容与空白, 因此标注 "故宫" 能命中标题 "故宫博物院（紫禁城）"。
    """
    target = RagService._normalize_name(section)
    if not target:
        return False
    for header in _HEADER_RE.findall(doc.page_content):
        norm = RagService._normalize_name(header)
        if norm and (target == norm or target in norm or norm in target):
            return True
    return False


def score_item(rag, item: dict) -> tuple:
    """评分一条正样本, 返回 (recall, mrr, 输出行列表)"""
    docs = knowledge_docs(rag, item["query"], item["city"])
    relevant = item.get("relevant", [])

    hit = 0
    first_rank = 0
    lines = []
    for rel in relevant:
        rank = next((i for i, d in enumerate(docs, 1) if matches(d, rel)), 0)
        if rank:
            hit += 1
            if first_rank == 0 or rank < first_rank:
                first_rank = rank
        lines.append(f"       {'✓' if rank else '✗'} {rel}" + (f" (rank {rank})" if rank else " (未召回)"))

    recall = hit / len(relevant) if relevant else 0.0
    mrr = 1.0 / first_rank if first_rank else 0.0

    lines.append("       检索结果:")
    for i, doc in enumerate(docs, 1):
        mark = "✓" if any(matches(doc, r) for r in relevant) else " "
        lines.append(f"         {i}.[{mark}] {doc.metadata.get('source')} | {named_sections(doc)}")
    return recall, mrr, lines


def check_negative(rag, item: dict) -> tuple:
    """检查一条负样本, 返回 (passed, vacuous, 输出行列表)"""
    query, city = item["query"], item["city"]
    docs = knowledge_docs(rag, query, city)
    leaked = sorted({d.metadata.get("city") for d in docs if d.metadata.get("city") != city})
    passed = not leaked

    # 反向验证: 不加城市过滤时, 该 query 是否真会召回别的城市?
    # 若不会, 这条负样本是空转的 —— 就算 filter 被删掉也照样"通过", 不算有效信号。
    # 注: 这里直接用了 _knowledge_store 私有属性, 只为在评测里复现"无过滤"对照;
    # 属性改名时降级为"无法验证", 不影响主流程。
    vacuous = None
    try:
        unfiltered = rag._knowledge_store.similarity_search(query, k=K)
        vacuous = not any(d.metadata.get("city") != city for d in unfiltered)
    except Exception:
        pass

    lines = []
    if passed:
        lines.append(f"       ✓ 未串城 (返回 {len(docs)} 条, 均属{city})")
    else:
        lines.append(f"       ✗ 串城! 混入: {', '.join(leaked)}")
    if vacuous is True:
        lines.append("       ⚠ 空转: 不过滤也未召回其他城市, 该条无守卫价值")
    elif vacuous is None:
        lines.append("       ? 无法验证是否空转 (私有属性不可用)")
    return passed, vacuous, lines


def evaluate(rag) -> None:
    items = load_eval_set()
    by_group = {g: [i for i in items if i.get("group") == g] for g, _ in GROUPS}

    print(f"知识库检索评测 | top-k = {K} | 用例 {len(items)} 条\n")

    for group, title in GROUPS:
        group_items = by_group[group]
        if not group_items:
            continue
        print("=" * 64)
        print(f"【{title}】{len(group_items)} 条")
        print("=" * 64)

        for i, item in enumerate(group_items, 1):
            print(f"\n[{i}] query={item['query']!r}  city={item['city']}")
            if group == "negative":
                passed, vacuous, lines = check_negative(rag, item)
                for line in lines:
                    print(line)
            else:
                recall, mrr, lines = score_item(rag, item)
                print(f"       Recall@{K}={recall:.2f}  MRR={mrr:.2f}")
                for line in lines:
                    print(line)

        print()
        _print_summary(rag, group, group_items)

    print("=" * 64)
    print("说明: 简单组从 1.00 掉下来 = 索引/切块/过滤坏了, 优先查那里;")
    print("      难组的 MRR 才是可以拿来做优化对比的数字。")
    print("=" * 64)


def _print_summary(rag, group: str, group_items: list) -> None:
    """打印单组小结"""
    if group == "negative":
        results = [check_negative(rag, i) for i in group_items]
        passed = sum(1 for p, _, _ in results if p)
        vacuous = sum(1 for _, v, _ in results if v is True)
        unknown = sum(1 for _, v, _ in results if v is None)
        print(f"小计: 通过 {passed}/{len(results)}", end="")
        if vacuous:
            print(f" (其中 {vacuous} 条空转, 建议换 query)", end="")
        if unknown:
            print(f" ({unknown} 条无法验证空转)", end="")
        print()
        return

    scores = [score_item(rag, i) for i in group_items]
    avg_recall = sum(r for r, _, _ in scores) / len(scores)
    avg_mrr = sum(m for _, m, _ in scores) / len(scores)
    print(f"小计: 平均 Recall@{K}={avg_recall:.2f}  平均 MRR={avg_mrr:.2f}")

    if group == "easy":
        missed = [i["query"] for i, (r, _, _) in zip(group_items, scores) if r < 1.0]
        if missed:
            print(f"      ⚠ 回归基线被打破! 未全中的 query: {', '.join(missed)}")
        else:
            print("      ✓ 基线保持全中 (预期如此, 无需关注)")
    else:
        hard = [i["query"] for i, (_, m, _) in zip(group_items, scores) if m < 1.0]
        if hard:
            print(f"      未排第 1 的 query ({len(hard)} 条): {', '.join(hard)}")


def main() -> None:
    rag = get_rag_service()
    if not rag.enabled:
        print("❌ RAG 未启用: 请先配置 DASHSCOPE_API_KEY")
        sys.exit(1)
    rag.ensure_knowledge_index()
    evaluate(rag)


if __name__ == "__main__":
    main()
