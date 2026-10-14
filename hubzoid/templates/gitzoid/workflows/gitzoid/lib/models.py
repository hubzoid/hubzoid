"""What the model returns, as validated Pydantic models (hub.call_llm with
response_model). Code decides what to keep; the model only proposes."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _one_of(allowed: tuple[str, ...], default: str, aliases: dict | None = None) -> BeforeValidator:
    """A closed set the model picks from. The schema lists the choices as JSON
    strings, which keeps models from writing bare words (`"confidence": high`
    is not JSON); an unexpected word becomes `default` instead of failing the
    whole answer."""
    def pick(value):
        word = str(value or "").strip().lower()
        word = (aliases or {}).get(word, word)
        return word if word in allowed else default
    return BeforeValidator(pick)


LEVELS = ("high", "medium", "low")
Severity = Annotated[Literal["high", "medium", "low"], _one_of(LEVELS, "medium", {"critical": "high"})]
Confidence = Annotated[Literal["high", "medium", "low"], _one_of(LEVELS, "low")]
Category = Annotated[Literal["bug", "security", "tests", "convention"],
                     _one_of(("bug", "security", "tests", "convention"), "bug")]
VerdictWord = Annotated[Literal["confirmed", "refuted", "uncertain"],
                        _one_of(("confirmed", "refuted", "uncertain"), "uncertain")]
AuditKind = Annotated[Literal["authorization", "secret", "backdoor", "injection", "other"],
                      _one_of(("authorization", "secret", "backdoor", "injection", "other"), "other")]


class Finding(BaseModel):
    path: str = Field("", description="File path from the diff, or empty for a concern about the whole change")
    line: int | None = Field(None, description="The new-file line number shown in the diff, or null")
    severity: Severity = Field(description="As the review guide defines them")
    confidence: Confidence = Field(description="How sure you are this is real on this line")
    category: Category
    body: str = Field(description="One or two plain sentences: the problem and what it breaks. No praise")
    rule: str = Field("", description="The team rule, repo instruction or check this rests on, if any")
    suggestion: str = Field("", description="Only when the fix is a small mechanical change to this one line: "
                            "the corrected line exactly as it should read, with its indentation. Otherwise empty")


class Review(BaseModel):
    summary: str = Field(description="One or two sentences a non-author understands: what this change does and why")
    findings: list[Finding] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list, description="At most 3 small non-blocking improvements")


class Verdict(BaseModel):
    index: int = Field(description="The finding's number from the list")
    verdict: VerdictWord
    severity: Severity = Field(description="After seeing the whole file")
    reason: str = Field(description="One sentence citing the code that decides it")


class Verification(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


class Resolution(BaseModel):
    id: str
    resolved: bool = False
    evidence: str = Field("", description="Quote current code and explain why the original failure can no longer occur")


class Resolutions(BaseModel):
    items: list[Resolution] = Field(default_factory=list)


class DependencyAssessment(BaseModel):
    advisory: str = Field(description="The advisory id exactly as given")
    keep: bool = Field(description="false only when the code shows the vulnerable feature cannot be reached")
    impact: str = Field(description="One or two plain sentences: what in this repo is affected, and how")
    fix: str = Field(description="The upgrade to make, as a version pin or command")


class DependencyReview(BaseModel):
    items: list[DependencyAssessment] = Field(default_factory=list)


class AuditFinding(BaseModel):
    path: str
    line: int | None = None
    kind: AuditKind
    severity: Severity
    confidence: Confidence
    body: str = Field(description="One or two plain sentences: the problem, who can exploit it and how")
    evidence: str = Field("", description="The line or lines of code that show it, quoted")


class Audit(BaseModel):
    findings: list[AuditFinding] = Field(default_factory=list)


class ProfilePart(BaseModel):
    name: str
    path: str = ""
    purpose: str


class Profile(BaseModel):
    what_it_is: str = Field(description="Two or three sentences: what this repo is and who uses it")
    parts: list[ProfilePart] = Field(default_factory=list)
    sign_in: str = Field("", description="Where sign-in and permission checks live, with file paths")
    data: str = Field("", description="The data stores it uses and what sensitive data it holds")
    external_services: list[str] = Field(default_factory=list)
    sensitive_areas: list[str] = Field(default_factory=list, description="Paths a reviewer should treat with care")


class DigestArea(BaseModel):
    name: str = Field(description="A product area or project, in the team's words")
    changes: list[str] = Field(description="What changed, one plain sentence each, with the pull request number")


class Digest(BaseModel):
    headline: str = Field(description="One sentence: the most important change merged; deployment is unknown")
    for_leadership: list[str] = Field(description="Three to five plain sentences on outcomes, not code")
    technical: list[str] = Field(default_factory=list, description="Up to six notes for engineers: notable code changes and risks")
    areas: list[DigestArea] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list, description="Risks or follow-ups worth a look, if any")
    poem: str = Field("", description="Only when asked: four short lines about the week")


class ChangeEvidence(BaseModel):
    change: str = Field(description="What the shown code changes, citing paths; distinguish intended behavior from proven outcomes")
    risks: list[str] = Field(default_factory=list, description="Specific risks or unknowns with file references")
    tests: str = Field("", description="Tests shown in the evidence, and what remains unverified; never infer test execution")
    follow_ups: list[str] = Field(default_factory=list, description="Concrete next checks supported by the diff")


class ReportNarrative(BaseModel):
    headline: str = Field(description="One sentence that quotes the most important number")
    highlights: list[str] = Field(default_factory=list, description="Up to five sentences, each quoting numbers given")
    watch: list[str] = Field(default_factory=list, description="Up to three things to look into, from the numbers")


# ------------------------------------------------------------ reading an answer

_CHOICES = ("high", "medium", "low", "confirmed", "refuted", "uncertain", "bug", "security", "tests", "convention",
            "authorization", "secret", "backdoor", "injection", "other")
_BARE_CHOICE = re.compile(r'("(?:severity|confidence|category|verdict|kind)"\s*:\s*)(' + "|".join(_CHOICES)
                          + r')(\s*[,}])')


def parse_answer(text: str, model: type[BaseModel]) -> BaseModel:
    """A model's text answer as `model`. Some models write a choice as a bare
    word (`"confidence": high`), which is not JSON; only that is repaired, and
    only for the choice fields above. Raises ValueError when it still can't be
    read."""
    from hubzoid.structured import ModelOutputError, extract_json
    try:
        data = extract_json(text)
    except ModelOutputError:
        data = extract_json(_BARE_CHOICE.sub(r'\1"\2"\3', text))
    return model.model_validate(data)
