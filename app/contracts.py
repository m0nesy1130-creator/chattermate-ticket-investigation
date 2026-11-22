"""各阶段交换的数据。证据和结论通过证据 ID 关联。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TaskRequest:
    question: str


@dataclass(frozen=True)
class Plan:
    query: str


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source: str
    line: int | None
    text: str
    version: str
    record: str | None = None
    title: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None


@dataclass(frozen=True)
class Claim:
    text: str
    evidence_ids: tuple[str, ...]


ReviewStatus = Literal["supported", "inference", "insufficient", "conflicting"]


@dataclass(frozen=True)
class ReviewItem:
    question: str
    status: ReviewStatus
    answer: str = ""
    evidence_ids: tuple[str, ...] = ()
    query: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


@dataclass(frozen=True)
class Review:
    status: ReviewStatus
    note: str
    rejected: tuple[RejectedDirection, ...] = ()
    items: tuple[ReviewItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rejected", tuple(self.rejected))
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class RejectedDirection:
    direction: str
    reason: str
    evidence_id: str
    quote: str


@dataclass(frozen=True)
class PlanStep:
    id: str
    question: str
    depends_on: tuple[str, ...]
    queries: tuple[str, ...]
    kind: Literal["document", "contract_fee", "mysql_payments", "balance", "adaptive"] = "document"
    preferred_tool: str = "document"

    def __post_init__(self) -> None:
        # 检查点反序列化时会将序列还原成列表。
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "queries", tuple(self.queries))


@dataclass(frozen=True)
class Money:
    amount: str
    currency: str


@dataclass(frozen=True)
class StepResult:
    step_id: str
    revision: int
    claim: Claim | None
    review: Review
    evidence: tuple[Evidence, ...]
    attempted_queries: tuple[str, ...]
    reviewed_at: str
    money: Money | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "attempted_queries", tuple(self.attempted_queries))
