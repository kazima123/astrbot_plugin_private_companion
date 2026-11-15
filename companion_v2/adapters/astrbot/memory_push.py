"""把 V2 已完成的记录交给注册的 writer，未注册时兼容 MemoryCompanion。

通用 writer 接收完整记录及原始会话身份，不使用旧桥的专用方法或内容截断。
只有没有活动 writer 时才进入下面的兼容路径，调用失败不切换提供方。

句柄来自 :func:`companion_v2.adapters.astrbot.memory_owner.read_memory_owner`：它读
牢插件自己公开的声明，这里拿同一次探测交回的句柄调用它公开的 ``record_*`` 方法。
没有第二个 ``sys.modules`` 扫描，也没有第二遍注册表读取。

写什么、怎么归类和怎么召回由牢插件决定，这里只做翻译：把一条
:class:`~companion_v2.capabilities.memory_push.MemoryPushRecord` 摊成那两个方法的
关键字。**不读它的库、不改它的状态、不替它做归属判定**——判定失败时它给的就是
「V2 自己来」，此处便不推。

这两个方法（``record_persona_life`` / ``record_proactive_message``）是 V1 在线上
用过的形状，所以默认值留给牢插件自己填：这里的 ``**kwargs`` 只覆盖 V2 真正知道的
事实，不重复声明 ``memory_type`` / ``visibility`` / ``reality_level``。
"""

from __future__ import annotations

import logging

from companion_v2.adapters.astrbot.memory_owner import read_memory_owner
from companion_v2.adapters.astrbot.memory_providers import (
    PROVIDER_API_VERSION, memory_context_document, memory_provider_registry,
)
from companion_v2.capabilities.memory_push import (
    PUSH_PERSONA_LIFE,
    PUSH_PROACTIVE_MESSAGE,
    MemoryPushRecord,
)


logger = logging.getLogger(__name__)

PUSH_SOURCE_PLUGIN = "astrbot_plugin_private_companion"
"""牢插件用它区分来源；必须稳定，重复推送同一条记录时不能被改写。"""

CONTENT_LIMIT = 4000
"""牢插件对一条记忆正文的上限（``clean_text(content, 4000)``）：超出部分在此截断。

截断放在适配层而不是核心：多长算「一条能召回的记录」是接收方的限制，V2 的领域
记录本身不该为了别家的实现缩短。
"""

EXCERPT_SUFFIX = "\n（以上仅为开头节选，完整内容见陪伴小屋原记录。）"

BRIDGE_METHODS = {
    PUSH_PERSONA_LIFE: "record_persona_life",
    PUSH_PROACTIVE_MESSAGE: "record_proactive_message",
}
"""记录种类 → 牢插件公开的写入方法；只有这一张映射表，没有兜底方法。"""


class AstrBotMemoryPush:
    """窄端口实现：探一次句柄，调一个 ``record_*``，别的什么都不做。"""

    def __init__(self, context=None):
        self._context = context

    async def push(self, record: MemoryPushRecord) -> str | None:
        providers = memory_provider_registry(self._context)
        entry = providers.select("write") if providers is not None else None
        if entry is not None:
            result = await providers.invoke(entry, "write", {
                **record.document(), "schema": PROVIDER_API_VERSION, "source_plugin": PUSH_SOURCE_PLUGIN,
                "context": memory_context_document(record.context) if record.context is not None else None})
            return result if isinstance(result, str) and result else None
        owner = read_memory_owner(self._context)
        if not owner.ownership.delegated or owner.handle is None:
            # 牢插件不在／被停用／句柄读不到：当场放弃，不缓冲也不重试。V2 自己的库
            # 才是原始记录，补写是维护工作，不该混进正常推送路径里无限堆积。
            return None
        method = getattr(owner.handle, BRIDGE_METHODS[record.kind], None)
        if not callable(method):
            # 声明还在，但它这一版没有这个方法：当作对方没提供，不猜别的接口，
            # 也不退回写它的其它 API。
            return None
        result = await method(content=_content(record), **self._arguments(record))
        return result if isinstance(result, str) and result else None

    @staticmethod
    def _arguments(record: MemoryPushRecord) -> dict:
        """只放 V2 真正知道的事实；缺的字段交给牢插件自己的默认值。"""

        arguments = {
            "memory_id": record.memory_id,
            "source_plugin": PUSH_SOURCE_PLUGIN,
            "scope": record.scope,
            "session_id": record.session_id,
            "platform": record.platform,
            "tags": list(record.tags),
            "metadata": dict(record.metadata),
        }
        if len(record.content.strip()) > CONTENT_LIMIT:
            arguments["metadata"].update(content_truncated=True, original_chars=len(record.content.strip()))
        if record.occurred_at:
            arguments["occurred_at"] = record.occurred_at
        if record.confidence is not None:
            arguments["confidence"] = record.confidence
        if record.importance is not None:
            arguments["importance"] = record.importance
        if record.context is not None:
            scope = record.context.scope
            if scope.kind.value in ("private", "group") and scope.is_resolved:
                private = scope.kind.value == "private"
                message_type = "FriendMessage" if private else "GroupMessage"
                arguments.update(
                    scope=scope.kind.value,
                    session_id=f"{scope.platform}:{message_type}:{scope.session_id}",
                    platform=scope.platform,
                    subject={"kind": "bot", "id": scope.account_id, "role": "bot_self"},
                    object={"kind": "user" if private else "group", "id": scope.native_scope_id,
                            "role": "peer_user" if private else "target_group"},
                    group_id="" if private else scope.native_scope_id,
                )
                arguments["metadata"].update(
                    bot_id=scope.account_id, owner_bot_id=scope.account_id,
                    companion_bot_id=scope.bot_id,
                    companion_role_id=record.context.role_binding.role.subject_id,
                    participant_user_id=scope.native_scope_id if private else "",
                    sender_name="Bot",
                )
        return arguments


def _content(record: MemoryPushRecord) -> str:
    text = record.content.strip()
    if len(text) <= CONTENT_LIMIT:
        return text
    return text[:CONTENT_LIMIT - len(EXCERPT_SUFFIX)] + EXCERPT_SUFFIX


def memory_push_provider(context=None):
    """组装用的窄端口：与 ``memory_owner_provider(context)`` 共用同一个 context。"""

    return AstrBotMemoryPush(context)


__all__ = [
    "AstrBotMemoryPush",
    "BRIDGE_METHODS",
    "CONTENT_LIMIT",
    "PUSH_SOURCE_PLUGIN",
    "memory_push_provider",
]
