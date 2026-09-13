"""``submit_tasks`` 工具的 JSON Schema —— 全系统唯一真相。

约束设计（PLAN.md §10）：

* ``required`` 只有 4 个核心字段。``notes`` / ``source_quote`` 选填，
  没有内容时模型就不输出这个键，避免为了填满 schema 而吐空字符串。
  曾经把这三个字段（含已删除的 ``confidence``）设为必填，是纯浪费 token。
* 没有 ``confidence``：LLM 自评置信度校准极差，几乎总是 0.85+，
  提供不了判别力。
* ``additionalProperties: false`` + 严格的 ``enum``，让约束解码器
  替我们挡住大部分格式漂移；语义规则（二选一）仍由服务端强制。
"""

from __future__ import annotations

TOOL_NAME = "submit_tasks"

CATEGORY_ENUM = ["homework", "practice", "exam", "appointment", "other"]

MAX_ITEMS = 20
TITLE_MAX = 20
NOTES_MAX = 60
QUOTE_MAX = 20

SUBMIT_TASKS_TOOL: dict = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "提交从图片或文本中抽取出的全部事项。必须调用本工具，不要输出解释性文字。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": MAX_ITEMS,
                    "description": "抽取出的事项；没有识别到任何事项时返回空数组",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "category", "due_at", "priority"],
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": f"≤{TITLE_MAX}字，不含时间、地点、课程名（已由其它字段承载）",
                            },
                            "category": {
                                "type": "string",
                                "enum": CATEGORY_ENUM,
                                "description": "作业 / 练习 / 考试 / 要约 / 其他",
                            },
                            "due_at": {
                                "type": ["string", "null"],
                                "description": "ISO8601 且带时区偏移；没有明确截止时间时为 null",
                            },
                            "priority": {
                                "type": ["integer", "null"],
                                "minimum": 1,
                                "maximum": 5,
                                "description": "仅在 due_at 为 null 时给出：1=Ⅰ 最紧急 … 5=Ⅴ 最不紧急",
                            },
                            "notes": {
                                "type": "string",
                                "description": f"≤{NOTES_MAX}字；没有额外信息就不要输出这个键",
                            },
                            "source_quote": {
                                "type": "string",
                                "description": f"≤{QUOTE_MAX}字，图片或文本中支撑该判定的原文片段",
                            },
                        },
                    },
                }
            },
        },
    },
}


def forced_tool_choice() -> dict:
    """强制模型调用 ``submit_tasks``。"""
    return {"type": "function", "function": {"name": TOOL_NAME}}
