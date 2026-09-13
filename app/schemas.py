"""请求/响应模型。

关键约束（PLAN.md §6）：

* ``due_at`` 与 ``priority`` **严格二选一**。数据库有 CHECK 约束兜底，
  这里在入口处就拦掉，给出可读的中文错误而不是 500。
* 两者都不给也拒绝。UI 与 LLM 后处理负责在更早的环节补默认值（Ⅲ），
  API 本身保持严格——这样快捷指令和未来的 agent 调用不会静默塞进
  "既没时间也没优先级"的悬空任务。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal["homework", "practice", "exam", "appointment", "other"]
ItemStatus = Literal["open", "done", "cancelled"]
ItemSource = Literal["web", "shortcut", "llm", "api"]
TaskView = Literal["ordered", "unordered"]

CATEGORY_VALUES: tuple[str, ...] = ("homework", "practice", "exam", "appointment", "other")

XOR_MESSAGE = "due_at 与 priority 必须二选一：有截止时间就不给优先级，没有截止时间才给优先级"


class _ItemFields(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)
    category: Category = "other"

    @field_validator("title", "notes", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        # 必须在 min_length 之前 strip，否则 "   " 会先通过长度校验再被压成空串
        return value.strip() if isinstance(value, str) else value


class ItemCreate(_ItemFields):
    """创建任务。"""

    due_at: datetime | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    source: ItemSource = "web"
    client_uuid: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _exactly_one_of_due_or_priority(self) -> "ItemCreate":
        if (self.due_at is None) == (self.priority is None):
            raise ValueError(XOR_MESSAGE)
        return self


class ItemUpdate(BaseModel):
    """局部更新。只处理显式提供的字段（``model_fields_set``）。

    语义约定：把 ``due_at`` 设为非空会**自动清空** ``priority``，反之亦然。
    这对应"填了截止时间就忽略优先级"的领域规则，避免客户端还要自己记得清另一个。
    但同一次请求里同时显式给出两个非空值仍然报错。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    category: Category | None = None
    due_at: datetime | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    status: ItemStatus | None = None

    @field_validator("title", "notes", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _not_both_explicit(self) -> "ItemUpdate":
        provided = self.model_fields_set
        if "due_at" in provided and "priority" in provided:
            if self.due_at is not None and self.priority is not None:
                raise ValueError(XOR_MESSAGE)
            if self.due_at is None and self.priority is None:
                raise ValueError(XOR_MESSAGE)
        return self


class ItemOut(BaseModel):
    """任务的对外表示。时间字段一律是存储用的 UTC ISO8601 字符串。"""

    id: int
    title: str
    notes: str
    category: str
    due_at: str | None
    priority: int | None
    status: str
    source: str
    client_uuid: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row) -> "ItemOut":
        return cls(**{key: row[key] for key in cls.model_fields if key in row.keys()})


class DeleteResult(BaseModel):
    deleted: bool
    id: int
