from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


GLOBAL_DEFAULT_EXECUTION_PROFILE = "default-python"
GLOBAL_DEFAULT_EXECUTION_RUNNER = "python"
DEFAULT_EXECUTION_PROFILES_BY_KIND: Dict[str, str] = {
    "workflow": "default-python",
    "compound_workflow": "default-python",
    "analysis": "python-sci",
    "codegen": "codegen-base",
    "review": "codegen-base",
}


@dataclass(frozen=True)
class ResolvedSkillExecution:
    runner: str
    profile: str
    confirmation: str
    source: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "runner": self.runner,
            "profile": self.profile,
            "confirmation": self.confirmation,
            "source": self.source,
        }


def _normalize_confirmation(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"", "default"}:
        return "always"
    if normalized in {"always", "never", "on_first_use"}:
        return normalized
    return "always"



def resolve_skill_execution(
    manifest: Optional[Mapping[str, Any]],
    *,
    global_default_profile: str = GLOBAL_DEFAULT_EXECUTION_PROFILE,
    global_default_runner: str = GLOBAL_DEFAULT_EXECUTION_RUNNER,
    kind_defaults: Optional[Mapping[str, str]] = None,
) -> ResolvedSkillExecution:
    manifest_dict = dict(manifest or {})
    execution = manifest_dict.get("execution") or {}
    if not isinstance(execution, Mapping):
        execution = {}
    kind = str(manifest_dict.get("kind") or "workflow").strip() or "workflow"
    kind_defaults = dict(kind_defaults or DEFAULT_EXECUTION_PROFILES_BY_KIND)

    if execution.get("profile"):
        return ResolvedSkillExecution(
            runner=str(execution.get("runner") or global_default_runner),
            profile=str(execution["profile"]),
            confirmation=_normalize_confirmation(execution.get("confirmation")),
            source="skill",
        )

    if kind in kind_defaults:
        return ResolvedSkillExecution(
            runner=str(execution.get("runner") or global_default_runner),
            profile=str(kind_defaults[kind]),
            confirmation=_normalize_confirmation(execution.get("confirmation")),
            source="kind_default",
        )

    return ResolvedSkillExecution(
        runner=str(execution.get("runner") or global_default_runner),
        profile=str(global_default_profile),
        confirmation=_normalize_confirmation(execution.get("confirmation")),
        source="global_default",
    )
