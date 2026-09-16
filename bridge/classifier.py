"""Fast deterministic Chat / Operation / Project routing.

The classifier deliberately does not call an LLM.  It returns a bounded
classification envelope; ambiguous turns are marked for ``bridge.routing``.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Mode(str, enum.Enum):
    CHAT = "chat"
    OPERATION = "operation"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class Classification:
    mode: Mode
    reason: str
    confidence: float
    source: str = "deterministic"
    reason_codes: tuple[str, ...] = ()
    task_intent: str = "none"
    needs_subagent: bool = False
    mutation_blocked: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "source": self.source,
            "reason_codes": list(self.reason_codes),
            "task_intent": self.task_intent,
            "needs_subagent": self.needs_subagent,
            "mutation_blocked": self.mutation_blocked,
        }


# ASCII boundaries are intentional: Python's ``\\b`` treats CJK adjacency
# differently from the product routing requirement.  These lookarounds keep
# ``项目project`` and ``project进行`` valid while avoiding ``my-project-x``.
PROJECT_TOKEN = r"(?<![A-Za-z0-9_-])(?:project|multi[- ]file|end[- ]to[- ]end)(?![A-Za-z0-9_-])"
PROJECT_ACTION = r"开发|实现|修改|修复|重构|创建|搭建|接入|迁移|部署|上线|发布|完成|构建"
PROJECT_OBJECT = r"项目|功能|代码|页面|服务|脚本|仓库|应用|网站|程序|project"
PROJECT_DELIVERY = r"测试|构建|启动|验收|交付|上线|发布|复核|部署"
PROJECT_SCOPE = r"项目级|完整交付|按项目流程|多文件|持续迭代|端到端|end[- ]to[- ]end"

OPERATION_PATTERNS = (
    r"\b(read|look up|query|fetch|send|rename|open|inspect|check|report)\b",
    r"读取|查看|查询|搜索|发送|打开|重命名|理解.{0,12}(文件|方案|文档)|报告|汇报|进度|状态|日志|检查|排查|诊断|记录",
)
CHAT_PATTERNS = (
    r"\b(explain|summarize|what is|why|how does|analyze|analyse|advise)\b",
    r"解释|总结|概括|什么是|为什么|怎么理解|聊聊|分析|判断|建议|是不是|是否",
)


def _has(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify(text: str) -> Classification:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return Classification(Mode.CHAT, "empty message", 1.0, reason_codes=("empty",))

    lowered = normalized.lower()
    has_token = bool(re.search(PROJECT_TOKEN, lowered, re.IGNORECASE))
    has_action = bool(re.search(PROJECT_ACTION, lowered, re.IGNORECASE))
    has_object = bool(re.search(PROJECT_OBJECT, lowered, re.IGNORECASE))
    has_delivery = bool(re.search(PROJECT_DELIVERY, lowered, re.IGNORECASE))
    has_scope = bool(re.search(PROJECT_SCOPE, lowered, re.IGNORECASE))
    operation_hit = _has(OPERATION_PATTERNS, lowered)
    chat_hit = _has(CHAT_PATTERNS, lowered)

    reasons: list[str] = []
    if has_token:
        reasons.append("explicit_project_token")
    if has_action:
        reasons.append("project_action")
    if has_object:
        reasons.append("project_object")
    if has_delivery:
        reasons.append("delivery_or_verification")
    if has_scope:
        reasons.append("explicit_project_scope")

    # A question/advice turn wins over a bare mention of "project".  A
    # requested action plus an object or a delivery/verification signal is
    # deterministic Project; a bare project token still marks an explicit
    # project context unless it is clearly conversational.
    strong_project = (
        (has_action and has_object)
        or (has_action and has_delivery)
        or (has_scope and (has_object or has_delivery))
        or (has_token and (has_action or has_delivery or has_scope))
        or (has_token and not chat_hit and not operation_hit)
    )
    if strong_project and not (chat_hit and not has_action and not has_delivery):
        return Classification(
            Mode.PROJECT,
            "matched deterministic project signals",
            0.92 if (has_action and (has_object or has_delivery)) else 0.80,
            reason_codes=tuple(reasons),
            task_intent="start",
        )

    if operation_hit and not chat_hit:
        return Classification(
            Mode.OPERATION,
            "matched a bounded-operation rule",
            0.88,
            reason_codes=("operation_signal",),
            task_intent="inspect",
        )
    if chat_hit and not operation_hit:
        return Classification(
            Mode.CHAT,
            "matched a conversational rule",
            0.90,
            reason_codes=("chat_signal",),
        )

    return Classification(
        Mode.CHAT,
        "ambiguous deterministic route",
        0.50,
        reason_codes=tuple(reasons) or ("no_strong_route",),
        task_intent="inspect",
        needs_subagent=True,
    )
