from service_club.core.memory import MemoryManager
from service_club.core.memory.permanent_memory import PermanentMemoryManager


# 作用：汇总偏好、显式记忆、画像事实和行为习惯，生成可重建的用户画像摘要。
# 参数：无。
class UserProfileSynthesizer:
    # 作用：从多个事实视图合成会话画像并回写摘要；派生摘要不取代原始事实源。
    # 参数 session_id：用于隔离所有会话级事实与状态的唯一标识。
    # 参数 memory：提供会话记忆事实与混合检索能力的管理器。
    # 参数 permanent_memory：提供跨会话永久事实读写能力的管理器。
    # 参数 behavior_instincts：用于合成画像的有效行为习惯列表。
    def synthesize(
        self,
        *,
        session_id: str,
        memory: MemoryManager,
        permanent_memory: PermanentMemoryManager,
        behavior_instincts: list[dict] | None = None,
    ) -> dict[str, object]:
        preferences = permanent_memory.preferences(session_id)
        recent_memories = memory.recall(session_id, "", limit=8)
        profile_facts = memory.list_profile_facts(session_id, statuses=("current",))
        interaction_stats = memory.get_profile_interaction_stats(session_id)
        fragments: list[str] = []
        if preferences:
            fragments.append(
                "偏好：" + "；".join(f"{key}={value}" for key, value in preferences.items())
            )
        if recent_memories:
            fragments.append("显式记忆：" + "；".join(recent_memories))
        if profile_facts:
            fragments.append(
                "可证实画像："
                + "；".join(
                    f"{item['category']}/{item['fact_key']}="
                    f"{item['polarity']}:{item['value']}"
                    for item in profile_facts
                )
            )
        if behavior_instincts:
            fragments.append(
                "互动假设："
                + "；".join(str(item["content"]) for item in behavior_instincts)
            )
        summary = " | ".join(fragments) if fragments else "暂无稳定画像"
        memory.upsert_user_profile(session_id, summary)
        return {
            "session_id": session_id,
            "summary": summary,
            "preferences": preferences,
            "recent_memories": recent_memories,
            "facts": profile_facts,
            "interaction_stats": interaction_stats,
            "behavior_instincts": behavior_instincts or [],
        }
