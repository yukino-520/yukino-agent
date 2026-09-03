from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

CONCEPT_GROUPS: dict[str, tuple[str, ...]] = {
    "sleep": ("失眠", "睡不着", "难眠", "睡眠", "夜里醒", "熬夜", "休息不好"),
    "pressure": ("压力", "累", "疲惫", "撑不住", "崩溃", "负担", "喘不过气"),
    "loneliness": ("孤独", "寂寞", "一个人", "没人懂", "陪伴", "被丢下"),
    "anxiety": ("焦虑", "紧张", "担心", "慌", "害怕", "不安"),
    "work_study": ("工作", "上班", "同事", "学习", "考试", "作业", "学校"),
    "communication": ("建议", "分析", "倾听", "安慰", "追问", "直接", "温柔"),
    "preference": ("喜欢", "偏好", "讨厌", "不喜欢", "习惯", "称呼"),
}


# 作用：表示一条融合检索命中，保留总分、分通道得分和可解释原因。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“MemoryHit”相关的数据结构、异常类型或服务组件。
# 字段：id：该对象中的结构化字段。、content：该对象中的结构化字段。、source：该对象中的结构化字段。、score：该对象中的结构化字段。、importance：该对象中的结构化字段。、created_at：该对象中的结构化字段。、reasons：该对象中的结构化字段。、score_breakdown：该对象中的结构化字段。
class MemoryHit:
    id: int
    content: str
    source: str
    score: float
    importance: float
    created_at: float
    reasons: list[str] = field(default_factory=list)
    score_breakdown: dict[str, float] = field(default_factory=dict)

    # 作用：将检索命中及其评分证据转换为可审计字典。
    # 参数：无。
    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "importance": self.importance,
            "created_at": self.created_at,
            "reasons": list(self.reasons),
            "score_breakdown": dict(self.score_breakdown),
        }


# 作用：在关系事实候选上融合词法、中文片段、概念、向量和一跳图谱评分。
# 参数：无。
class HybridMemoryRetriever:
    """Local hybrid retrieval with an auditable no-embedding fallback."""

    MIN_SCORE = 0.14
    RECENCY_HALF_LIFE_DAYS = 30.0

    # 作用：绑定关系事实源及可选向量、Milvus、知识图谱派生通道。
    # 参数 store：持久化记忆、关系或学习事实的存储对象。
    # 参数 embedding_provider：可选的文本向量生成服务。
    # 参数 vector_store：可选的 Milvus 等外部向量派生索引。
    # 参数 knowledge_graph：可选的知识图谱事实存储或查询通道。
    # 参数 clock：提供当前时间戳的可注入时钟函数。
    def __init__(
        self,
        store: Any,
        *,
        embedding_provider: Any | None = None,
        vector_store: Any | None = None,
        search_index: Any | None = None,
        knowledge_graph: Any | None = None,
        clock=time.time,
    ) -> None:  # noqa: ANN001
        self.store = store
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.search_index = search_index
        self.knowledge_graph = knowledge_graph
        self._clock = clock

    # 作用：从关系库取候选并融合各通道排序，派生索引失败时仍可本地降级召回。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def search(self, session_id: str, query: str, limit: int = 5) -> list[MemoryHit]:
        if not query.strip():
            rows = self.store.list_memory_candidates(session_id, limit=max(limit * 4, 20))
            return self._rank_without_query(rows, limit)

        fetch_limit = max(50, min(limit * 20, 300))
        lexical_hits = (
            self.search_index.search_memories(session_id, query, limit=fetch_limit)
            if self.search_index is not None
            else []
        )
        semantic_scores = self._semantic_candidates(
            session_id, query, limit=fetch_limit
        )
        candidate_ids = list(
            dict.fromkeys(
                [int(hit.id) for hit in lexical_hits]
                + [memory_id for memory_id, _ in sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True)]
            )
        )
        if candidate_ids:
            rows = self.store.memory_candidates_by_ids(session_id, candidate_ids)
        else:
            rows = self.store.list_memory_candidates(session_id, limit=min(fetch_limit, 50))
        if not rows:
            return []

        lexical_ranks = {int(hit.id): int(hit.rank) for hit in lexical_hits}
        semantic_ranks = {
            memory_id: rank
            for rank, (memory_id, _) in enumerate(
                sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True),
                start=1,
            )
        }

        query_tokens = self._tokens(query)
        query_grams = self._cjk_ngrams(query)
        query_concepts = self._concepts(query)
        document_tokens = [self._tokens(str(row["content"])) for row in rows]
        document_frequency = {
            token: sum(token in tokens for tokens in document_tokens)
            for token in query_tokens
        }
        raw_lexical = [
            self._bm25(query_tokens, tokens, document_frequency, len(rows))
            for tokens in document_tokens
        ]
        lexical_max = max(raw_lexical, default=0.0) or 1.0
        now = self._clock()
        vector_scores = self._vector_scores(rows, query, now, session_id)
        vector_scores.update(semantic_scores)
        graph_scores = self._graph_scores(rows, query, session_id)
        has_vector_channel = bool(vector_scores)
        hits: list[MemoryHit] = []
        for row, _tokens, lexical_raw in zip(rows, document_tokens, raw_lexical):
            content = str(row["content"])
            lexical = lexical_raw / lexical_max
            char_similarity = self._dice(query_grams, self._cjk_ngrams(content))
            candidate_concepts = self._concepts(content)
            concept = self._jaccard(query_concepts, candidate_concepts)
            importance = min(1.0, max(0.0, float(row["importance"])))
            age_days = max(0.0, now - float(row["created_at"])) / 86400
            recency = 0.5 ** (age_days / self.RECENCY_HALF_LIFE_DAYS)
            exact = self._exact_signal(query, content)
            vector = vector_scores.get(int(row["id"]), 0.0)
            graph = graph_scores.get(int(row["id"]), 0.0)
            lexical_rrf = 1.0 / (60 + lexical_ranks[int(row["id"])]) if int(row["id"]) in lexical_ranks else 0.0
            vector_rrf = 1.0 / (60 + semantic_ranks[int(row["id"])]) if int(row["id"]) in semantic_ranks else 0.0
            rrf = (lexical_rrf + vector_rrf) * 30.5
            if has_vector_channel:
                score = (
                    0.24 * lexical
                    + 0.14 * char_similarity
                    + 0.12 * concept
                    + 0.18 * vector
                    + 0.1 * graph
                    + 0.07 * importance
                    + 0.03 * recency
                    + 0.02 * exact
                    + 0.10 * rrf
                )
            else:
                score = (
                    0.31 * lexical
                    + 0.18 * char_similarity
                    + 0.16 * concept
                    + 0.14 * graph
                    + 0.09 * importance
                    + 0.05 * recency
                    + 0.03 * exact
                    + 0.04 * rrf
                )
            if score < self.MIN_SCORE or not any(
                (lexical, char_similarity, concept, exact, vector >= 0.55, graph >= 0.55)
            ):
                continue
            breakdown = {
                "lexical": round(lexical, 4),
                "char_similarity": round(char_similarity, 4),
                "concept": round(concept, 4),
                "importance": round(importance, 4),
                "recency": round(recency, 4),
                "exact": round(exact, 4),
                "vector": round(vector, 4),
                "graph": round(graph, 4),
                "rrf": round(rrf, 4),
            }
            hits.append(
                MemoryHit(
                    id=int(row["id"]),
                    content=content,
                    source=str(row["source"]),
                    score=round(score, 4),
                    importance=importance,
                    created_at=float(row["created_at"]),
                    reasons=self._reasons(breakdown, query_concepts & candidate_concepts),
                    score_breakdown=breakdown,
                )
            )
        hits.sort(key=lambda item: (item.score, item.importance, item.created_at), reverse=True)
        return hits[:limit]

    def _semantic_candidates(
        self, session_id: str, query: str, *, limit: int
    ) -> dict[int, float]:
        provider = self.embedding_provider
        if provider is None or not provider.enabled or self.vector_store is None:
            return {}
        try:
            query_vector = provider.embed([query])[0]
            return self.vector_store.search(
                session_id=session_id,
                model=provider.model,
                query_vector=query_vector,
                candidate_ids=None,
                limit=limit,
            )
        except Exception:
            return {}

    # 作用：汇总当前启用的检索通道、派生索引状态和降级说明。
    # 参数：无。
    def status(self) -> dict[str, object]:
        provider_status = (
            self.embedding_provider.status()
            if self.embedding_provider is not None
            else {"enabled": False, "model": "", "last_error": ""}
        )
        channels = ["elasticsearch_bm25", "rrf", "cjk_char_ngram", "concept", "importance", "recency"]
        if provider_status["enabled"]:
            channels.append("embedding")
        vector_index = (
            self.vector_store.status()
            if self.vector_store is not None
            else {
                "ok": True,
                "enabled": False,
                "backend": getattr(self.store.backend, "name", "relational"),
                "connection_state": "disabled",
                "source_of_truth": "relational",
            }
        )
        if vector_index.get("enabled"):
            channels.append("milvus")
        graph_index = (
            self.knowledge_graph.status()
            if self.knowledge_graph is not None
            else {"ok": True, "enabled": False, "fact_backend": "unbound"}
        )
        if self.knowledge_graph is not None:
            channels.append("knowledge_graph")
        return {
            "mode": (
                "milvus_hybrid"
                if provider_status["enabled"] and vector_index.get("enabled")
                else "vector_hybrid" if provider_status["enabled"] else "local_hybrid"
            ),
            "channels": channels,
            "embedding_available": provider_status["enabled"],
            "embedding_model": provider_status["model"],
            "embedding_last_error": provider_status["last_error"],
            "vector_index": vector_index,
            "knowledge_graph": graph_index,
            "search_index": self.search_index.status() if self.search_index is not None else {"ok": False, "enabled": False},
            "degradation": (
                "" if provider_status["enabled"] and not provider_status["last_error"]
                else "向量服务未配置或失败时使用本地概念语义与字符片段召回"
            ),
            "min_score": self.MIN_SCORE,
        }

    # 作用：无查询文本时仅按事实重要度与时间衰减排序。
    # 参数 rows：待排序或转换的数据库记录集合。
    # 参数 limit：本次查询、返回或格式化允许的最大条数。
    def _rank_without_query(self, rows: list[dict[str, Any]], limit: int) -> list[MemoryHit]:
        now = self._clock()
        hits = []
        for row in rows:
            importance = min(1.0, max(0.0, float(row["importance"])))
            age_days = max(0.0, now - float(row["created_at"])) / 86400
            recency = 0.5 ** (age_days / self.RECENCY_HALF_LIFE_DAYS)
            score = 0.7 * importance + 0.3 * recency
            hits.append(
                MemoryHit(
                    id=int(row["id"]),
                    content=str(row["content"]),
                    source=str(row["source"]),
                    score=round(score, 4),
                    importance=importance,
                    created_at=float(row["created_at"]),
                    reasons=["无查询时按重要度与时效排序"],
                    score_breakdown={
                        "importance": round(importance, 4),
                        "recency": round(recency, 4),
                    },
                )
            )
        hits.sort(key=lambda item: (item.score, item.created_at), reverse=True)
        return hits[:limit]

    # 作用：补齐向量缓存并按 Milvus、数据库向量、进程内余弦的顺序降级打分。
    # 参数 rows：待排序或转换的数据库记录集合。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 now：调用方提供的当前时间戳，保证计算与持久化一致。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def _vector_scores(
        self,
        rows: list[dict[str, Any]],
        query: str,
        now: float,
        session_id: str,
    ) -> dict[int, float]:
        provider = self.embedding_provider
        if provider is None or not provider.enabled:
            return {}
        try:
            ids = [int(row["id"]) for row in rows]
            cached = self.store.get_memory_embeddings(ids, provider.model)
            missing = [row for row in rows if int(row["id"]) not in cached]
            if missing:
                vectors = provider.embed([str(row["content"]) for row in missing])
                for row, vector in zip(missing, vectors, strict=True):
                    memory_id = int(row["id"])
                    cached[memory_id] = vector
                    self.store.save_memory_embedding(
                        memory_id,
                        provider.model,
                        vector,
                        now=now,
                        session_id=session_id,
                    )
            query_vector = provider.embed([query])[0]
            if self.vector_store is not None:
                vector_scores = self.vector_store.search(
                    session_id=session_id,
                    model=provider.model,
                    query_vector=query_vector,
                    candidate_ids=ids,
                    limit=len(ids),
                )
                if not vector_scores and cached:
                    for memory_id, vector in cached.items():
                        self.vector_store.upsert(
                            memory_id=memory_id,
                            session_id=session_id,
                            model=provider.model,
                            vector=vector,
                            updated_at=now,
                        )
                    vector_scores = self.vector_store.search(
                        session_id=session_id,
                        model=provider.model,
                        query_vector=query_vector,
                        candidate_ids=ids,
                        limit=len(ids),
                    )
                if vector_scores:
                    return vector_scores
            server_scorer = getattr(self.store, "memory_vector_scores", None)
            if callable(server_scorer):
                server_scores = server_scorer(ids, provider.model, query_vector)
                if server_scores:
                    return server_scores
            return {
                memory_id: max(0.0, self._cosine(query_vector, vector))
                for memory_id, vector in cached.items()
            }
        except Exception:
            return {}

    # 作用：用知识图谱查询得到的种子及一跳节点标签为事实候选提供关联分。
    # 参数 rows：待排序或转换的数据库记录集合。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    def _graph_scores(
        self,
        rows: list[dict[str, Any]],
        query: str,
        session_id: str,
    ) -> dict[int, float]:
        if self.knowledge_graph is None or not query.strip():
            return {}
        try:
            graph = self.knowledge_graph.search(session_id, query, limit=50)
            seeds = {str(value) for value in graph.get("seed_node_ids", [])}
            labels = [
                (
                    str(node.get("label", "")).strip().casefold(),
                    1.0 if str(node.get("id", "")) in seeds else 0.62,
                )
                for node in graph.get("nodes", [])
                if str(node.get("label", "")).strip()
                and str(node.get("type", "")) != "person"
            ]
            scores: dict[int, float] = {}
            for row in rows:
                content = str(row["content"]).casefold()
                score = max(
                    (weight for label, weight in labels if label in content),
                    default=0.0,
                )
                if score:
                    scores[int(row["id"])] = score
            return scores
        except Exception:
            return {}

    # 作用：计算等长非零向量的余弦相似度，无效输入返回零分。
    # 参数 left：相似度计算左侧的集合或向量。
    # 参数 right：相似度计算右侧的集合或向量。
    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=True)) / (
            left_norm * right_norm
        )

    # 作用：提取英文词、中文 Bigram、短中文串和领域同义词作为词法特征。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _tokens(self, text: str) -> set[str]:
        normalized = text.lower()
        tokens = set(re.findall(r"[a-z0-9_]+", normalized))
        for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
            tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
            if len(sequence) <= 4:
                tokens.add(sequence)
        for words in CONCEPT_GROUPS.values():
            tokens.update(word for word in words if word in normalized)
        return {token for token in tokens if token}

    # 作用：提取纯中文字符的 Bigram，用于抵抗分词差异。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _cjk_ngrams(self, text: str) -> set[str]:
        compact = "".join(re.findall(r"[\u3400-\u9fff]", text))
        return {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}

    # 作用：将文本命中的领域词组映射为统一概念标签。
    # 参数 text：当前用户原话或待解析、估算、规范化的文本。
    def _concepts(self, text: str) -> set[str]:
        return {
            concept
            for concept, words in CONCEPT_GROUPS.items()
            if any(word in text for word in words)
        }

    # 作用：计算只使用 IDF 的 BM25 风格词法匹配分。
    # 参数 query_tokens：从查询文本提取的去重词项集合。
    # 参数 document_tokens：当前候选文档的去重词项集合。
    # 参数 document_frequency：查询词在候选文档集合中的出现文档数。
    # 参数 document_count：参与词法评分的候选文档总数。
    def _bm25(
        self,
        query_tokens: set[str],
        document_tokens: set[str],
        document_frequency: dict[str, int],
        document_count: int,
    ) -> float:
        score = 0.0
        for token in query_tokens & document_tokens:
            frequency = document_frequency.get(token, 0)
            score += math.log(1 + (document_count - frequency + 0.5) / (frequency + 0.5))
        return score

    # 作用：计算两组中文片段的 Dice 相似度。
    # 参数 left：相似度计算左侧的集合或向量。
    # 参数 right：相似度计算右侧的集合或向量。
    def _dice(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return 2 * len(left & right) / (len(left) + len(right))

    # 作用：计算两组概念标签的 Jaccard 相似度。
    # 参数 left：相似度计算左侧的集合或向量。
    # 参数 right：相似度计算右侧的集合或向量。
    def _jaccard(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    # 作用：检查完整查询或长度足够的查询片段是否直接出现在记忆中。
    # 参数 query：用于检索、匹配或遗忘的用户查询文本。
    # 参数 content：待保存、评分、格式化或发送的业务正文。
    def _exact_signal(self, query: str, content: str) -> float:
        terms = [term for term in re.split(r"[\s，。！？,.!?]+", query) if len(term) >= 2]
        return 1.0 if query in content or any(term in content for term in terms) else 0.0

    # 作用：把各通道得分转换为面向调试与用户解释的命中原因。
    # 参数 scores：各混合检索通道的归一化得分。
    # 参数 concepts：当前查询与候选共同命中的领域概念集合。
    def _reasons(self, scores: dict[str, float], concepts: set[str]) -> list[str]:
        reasons = []
        if scores["lexical"] > 0:
            reasons.append("关键词/BM25 命中")
        if scores["char_similarity"] >= 0.12:
            reasons.append("中文字符片段相似")
        if scores.get("vector", 0.0) >= 0.55:
            reasons.append("向量语义相似")
        if scores.get("graph", 0.0) >= 0.55:
            reasons.append("知识图谱关联")
        if concepts:
            reasons.append("概念相关:" + ",".join(sorted(concepts)))
        if scores["importance"] >= 0.7:
            reasons.append("高重要度")
        return reasons or ["时效与重要度补充"]
