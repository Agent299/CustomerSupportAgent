"""Golden dataset schema (docs/PLAN.md §11).

YAML in ``evals/golden/`` is the source of truth: reviewable in PRs, versioned with the code.
Each example states the expected route, and for answerable ones the docs pages that hold the
answer (``expected_sources``) and facts a correct answer must contain (``key_facts``).
``key_facts`` are validated against the ingested docs, so the set cannot drift from them.
"""

from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["faq", "complex", "error_code", "escalation"]
Route = Literal["ANSWERED", "ESCALATE"]
EscalationReason = Literal["billing", "account", "out_of_scope", "human_request", "security"]

GOLDEN_DIR = Path(__file__).parent / "golden"


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class GoldenExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    category: Category
    query: str = Field(min_length=3)  # the latest user message
    history: list[Turn] = Field(default_factory=list)  # earlier turns (multi-turn cases)
    version: Literal["9.0", "8.0", "7.0"] | None = None  # None = unspecified → latest
    expected_route: Route
    expected_sources: list[str] = Field(default_factory=list)  # doc keys; any one is a hit
    key_facts: list[str] = Field(default_factory=list)
    golden_answer: str = Field(min_length=10)
    escalation_reason: EscalationReason | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _route_consistency(self) -> "GoldenExample":
        if self.expected_route == "ANSWERED":
            if not self.expected_sources or not self.key_facts:
                raise ValueError(f"{self.id}: answerable examples need sources and key_facts")
            if self.escalation_reason is not None:
                raise ValueError(f"{self.id}: escalation_reason only applies to ESCALATE")
        else:
            if self.expected_sources:
                raise ValueError(f"{self.id}: ESCALATE examples have no expected_sources")
            if self.escalation_reason is None:
                raise ValueError(f"{self.id}: ESCALATE examples need an escalation_reason")
        if (self.category == "escalation") != (self.expected_route == "ESCALATE"):
            raise ValueError(f"{self.id}: category 'escalation' iff route ESCALATE")
        return self

    @property
    def effective_version(self) -> str:
        return self.version or "9.0"

    @property
    def search_text(self) -> str:
        """Retrieval input before Phase 2 query rewriting: the user's turns, oldest first."""
        user_turns = [t.content for t in self.history if t.role == "user"]
        return "\n".join([*user_turns, self.query])


class GoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    examples: list[GoldenExample]

    @model_validator(mode="after")
    def _unique_ids(self) -> "GoldenSet":
        duplicates = [i for i, n in Counter(e.id for e in self.examples).items() if n > 1]
        if duplicates:
            raise ValueError(f"duplicate example ids: {duplicates}")
        return self

    def by_category(self) -> dict[str, list[GoldenExample]]:
        groups: dict[str, list[GoldenExample]] = {}
        for example in self.examples:
            groups.setdefault(example.category, []).append(example)
        return groups


def load_golden(path: Path | None = None) -> GoldenSet:
    path = path or GOLDEN_DIR / "v0.yaml"
    return GoldenSet.model_validate(yaml.safe_load(path.read_text()))
