"""Checklist configuration: the single source of truth for evaluation.

Nothing about specific criteria is hard-coded elsewhere: prompts, validation
and scoring are all derived from a Checklist instance, so users can add,
edit or replace criteria through the UI.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

CHANNELS = ("agent chat", "voice call")


class CriterionType(str, Enum):
    BINARY = "binary"  # Да / Нет (+ N/A if allowed); "Да" gives the full weight
    SCALE = "scale"    # one of the listed steps (+ N/A if allowed); top step gives the full weight
    FLAG = "flag"      # gross-violation flag: Да / Нет, no points, reported separately


class ScaleStep(BaseModel):
    value: float
    description: str


class ChannelOverride(BaseModel):
    """Channel-specific wording (e.g. speech instead of writing for calls).

    Only wording can be overridden; type, weight and N/A availability stay
    the same so that scores remain comparable across channels.
    """
    title: str | None = None
    description: str | None = None
    na_condition: str | None = None
    guidance: str | None = None
    scale: list[ScaleStep] | None = None


class Criterion(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    section: str | None = None
    title: str
    type: CriterionType
    weight: float = Field(ge=0)
    description: str  # what a positive answer ("Да") means / what is evaluated
    na_allowed: bool = False
    na_condition: str | None = None  # when N/A is allowed to be set
    scale: list[ScaleStep] | None = None
    guidance: str | None = None  # extra rules for the evaluator
    channel_overrides: dict[str, ChannelOverride] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "Criterion":
        if self.type == CriterionType.SCALE:
            if not self.scale or len(self.scale) < 2:
                raise ValueError(f"{self.id}: scale criterion needs at least two steps")
            values = [s.value for s in self.scale]
            if len(set(values)) != len(values):
                raise ValueError(f"{self.id}: duplicate scale values")
            if max(values) <= 0:
                raise ValueError(f"{self.id}: top scale step must be > 0")
        elif self.scale:
            raise ValueError(f"{self.id}: only scale criteria may define a scale")
        if self.type == CriterionType.FLAG and self.weight != 0:
            raise ValueError(f"{self.id}: flag criteria carry no points (weight must be 0)")
        if self.na_allowed and not (self.na_condition or "").strip():
            raise ValueError(f"{self.id}: N/A is allowed but na_condition is empty")
        for ch, ov in self.channel_overrides.items():
            if ch not in CHANNELS:
                raise ValueError(f"{self.id}: unknown channel override '{ch}'")
            if ov.scale is not None:
                if self.type != CriterionType.SCALE:
                    raise ValueError(f"{self.id}: scale override on a non-scale criterion")
                if sorted(s.value for s in ov.scale) != sorted(s.value for s in self.scale):
                    raise ValueError(f"{self.id}: override scale must keep the same step values")
        return self

    @property
    def scale_max(self) -> float:
        return max(s.value for s in self.scale) if self.scale else 1.0

    def for_channel(self, channel: str | None) -> "Criterion":
        """Return a copy with the channel-specific wording applied."""
        ov = self.channel_overrides.get(channel or "")
        if not ov:
            return self
        patch = {k: v for k, v in ov.model_dump(exclude_none=True).items()}
        if "scale" in patch:
            patch["scale"] = ov.scale
        return self.model_copy(update=patch)


class Section(BaseModel):
    id: str
    title: str


class Checklist(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    name: str
    version: int = 1
    description: str | None = None
    channels: list[str] = Field(default_factory=lambda: list(CHANNELS))
    rounding: int = Field(default=2, ge=0, le=6)
    general_rules: list[str] = Field(default_factory=list)  # global instructions for the evaluator
    company_context: str | None = None  # optional product/company reference text
    sections: list[Section] = Field(default_factory=list)
    criteria: list[Criterion]

    @model_validator(mode="after")
    def _check(self) -> "Checklist":
        ids = [c.id for c in self.criteria]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate criterion ids: {sorted(dupes)}")
        section_ids = {s.id for s in self.sections}
        for c in self.criteria:
            if c.section and c.section not in section_ids:
                raise ValueError(f"{c.id}: unknown section '{c.section}'")
        for ch in self.channels:
            if ch not in CHANNELS:
                raise ValueError(f"unknown channel '{ch}'")
        if not any(c.enabled for c in self.criteria):
            raise ValueError("checklist has no enabled criteria")
        return self

    @property
    def active_criteria(self) -> list[Criterion]:
        return [c for c in self.criteria if c.enabled]

    def criterion(self, criterion_id: str) -> Criterion:
        for c in self.criteria:
            if c.id == criterion_id:
                return c
        raise KeyError(criterion_id)

    @classmethod
    def load(cls, path: str | Path) -> "Checklist":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
