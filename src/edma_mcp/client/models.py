from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional, Any, Literal, Dict
import json
import re


def _humanize_identifier(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^(agent_|tool_|param_)", "", value)
    value = value.replace("__", " ").replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value.capitalize() if value else ""


def _format_user_value(value: Any) -> str:
    if value is None:
        return "leave as current/default"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            return "use the result from an earlier step"
        if not stripped:
            return "leave blank"
        return stripped
    if isinstance(value, list):
        if not value:
            return "empty list"
        preview = ", ".join(_format_user_value(item) for item in value[:4])
        if len(value) > 4:
            preview += ", ..."
        return preview
    if isinstance(value, dict):
        if not value:
            return "empty object"
        preview_items = [f"{_humanize_identifier(k)}: {_format_user_value(v)}" for k, v in list(value.items())[:4]]
        preview = "; ".join(preview_items)
        if len(value) > 4:
            preview += "; ..."
        return preview
    return str(value)

class WorkflowState(BaseModel):
    """Global workflow state maintained in context."""
    user_goal: str = ""
    is_confirmed: bool = Field(False, description="Has the user explicitly confirmed the plan?")
    plan_steps: List[Dict[str, Any]] = Field(default_factory=list)
    current_step_index: int = 0
    completed_steps: List[str] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

class TriageToPlanner(BaseModel):
    """Initial handoff from Triage to Planner."""
    user_goal: str = Field(..., description="The user's original request or goal.")
    current_state: str = Field("", description="Optional context about the current system state.")
    constraints: str = Field("", description="Any constraints mentioned by the user.")
    success_criteria: str = Field("", description="What defines a successful completion.")

class PlanStep(BaseModel):
    """A detailed step defined by the Planner."""
    step_id: str = Field(..., description="Unique ID for the step (e.g., '1', 'setup').")
    agent: str = Field(..., description="The specific MCP agent to execute this step (e.g. 'agent_sem').")
    goal: str = Field(..., description="High-level goal of this specific step.")
    skill: str = Field(..., description="The ID of the skill/playbook to use.")
    tool_name: str = Field(..., description="Exact MCP tool/function name to call on the target agent.")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="JSON-serializable arguments for the MCP tool.")
    required_inputs: List[str] = Field(default_factory=list, description="Inputs to request only when execution reaches this step.")
    expected_output: str = Field(..., description="Criteria for successful completion of this step.")
    on_success: str = Field(..., description="Next step ID to go to on success (e.g. '2') or 'done'.")
    on_failure: str = Field(..., description="Next step ID to go to on failure (e.g. 'stop') or 'ask user'.")

    @field_validator("step_id", "agent", "goal", "skill", "tool_name", "expected_output", "on_success", "on_failure")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    def user_action_text(self) -> str:
        return self.goal

    def user_argument_lines(self) -> List[str]:
        if not self.arguments:
            return ["Use the current/default settings for this step."]
        lines: List[str] = []
        for key, value in self.arguments.items():
            lines.append(f"{_humanize_identifier(key)}: {_format_user_value(value)}")
        return lines

    def user_required_input_lines(self) -> List[str]:
        return [str(item).strip() for item in (self.required_inputs or []) if str(item).strip()]

    def user_next_step_text(self) -> str:
        if self.on_success == "done":
            return "Finish the workflow if this step succeeds."
        return f"Then continue to step {self.on_success} if this succeeds."

    def to_user_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.user_action_text(),
            "arguments": self.user_argument_lines(),
            "required_inputs": self.user_required_input_lines(),
            "next_step": self.user_next_step_text(),
            "expected_output": self.expected_output,
        }


class UserPlanStep(BaseModel):
    """LLM-authored user-facing explanation for one plan step."""
    step_id: str = Field(..., description="The step id matching the internal PlanStep.")
    action: str = Field(..., description="Plain-language description of what this step will do.")
    argument_guidance: List[str] = Field(default_factory=list, description="Plain-language explanation of the important settings or values for this step.")
    required_inputs: List[str] = Field(default_factory=list, description="What the user may need to provide or confirm at this step.")
    next_step: str = Field(..., description="Plain-language description of what happens after this step.")

    @field_validator("step_id", "action", "next_step")
    @classmethod
    def non_empty_user_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

class Plan(BaseModel):
    """A list of PlanSteps generated by the Planner."""
    steps: List[PlanStep] = Field(..., min_length=1, description="The sequence of steps to achieve the goal.")

    @model_validator(mode="after")
    def validate_step_references(self) -> "Plan":
        step_ids = {step.step_id for step in self.steps}
        terminal_refs = {"done", "stop", "ask_user"}
        for step in self.steps:
            for field_name in ("on_success", "on_failure"):
                ref = getattr(step, field_name)
                if ref not in terminal_refs and ref not in step_ids:
                    raise ValueError(f"{field_name} for step '{step.step_id}' points to unknown step '{ref}'")
        return self

class PlanReview(BaseModel):
    """Planner output shown to the user before any real MCP tool execution."""
    status: Literal["needs_approval"] = Field("needs_approval", description="Plan reviews must wait for user approval.")
    goal: str = Field(..., description="The user's workflow goal.")
    summary: str = Field(..., description="Short human-readable plan summary.")
    steps: List[PlanStep] = Field(..., min_length=1, description="Executable steps proposed by the planner.")
    user_summary: str = Field("", description="LLM-authored plain-language summary shown to the user.")
    user_steps: List[UserPlanStep] = Field(default_factory=list, description="LLM-authored plain-language plan steps shown to the user.")
    missing_inputs: List[str] = Field(default_factory=list, description="Deprecated top-level field. Prefer PlanStep.required_inputs so execution asks only when that step is reached.")
    risks: List[str] = Field(default_factory=list, description="Known risks or assumptions to review.")

    @model_validator(mode="after")
    def validate_plan_references(self) -> "PlanReview":
        Plan(steps=self.steps)
        if self.user_steps:
            step_ids = {step.step_id for step in self.steps}
            user_step_ids = [step.step_id for step in self.user_steps]
            unknown = [step_id for step_id in user_step_ids if step_id not in step_ids]
            if unknown:
                raise ValueError(f"user_steps reference unknown internal steps: {unknown}")
        return self

    def to_user_plan(self) -> Dict[str, Any]:
        if self.user_steps:
            return {
                "goal": self.goal,
                "summary": self.user_summary or self.summary,
                "steps": [
                    {
                        "step_id": step.step_id,
                        "action": step.action,
                        "arguments": list(step.argument_guidance or []),
                        "required_inputs": list(step.required_inputs or []),
                        "next_step": step.next_step,
                    }
                    for step in self.user_steps
                ],
                "risks": list(self.risks or []),
            }
        return {
            "goal": self.goal,
            "summary": self.summary,
            "steps": [step.to_user_dict() for step in self.steps],
            "risks": list(self.risks or []),
        }

    def to_user_plan_json(self) -> str:
        return json.dumps(self.to_user_plan(), ensure_ascii=False)

class ExecuteStepInput(BaseModel):
    """Instruction packet sent from Planner to Execute Agent."""
    step_id: str = Field(..., description="The ID of the step to execute.")
    agent: str = Field(..., description="The target specialist agent.")
    goal: str = Field(..., description="The specific goal for this step.")
    skill: str = Field(..., description="The skill or tool-set to be performed.")
    tool_name: str = Field(..., description="Exact MCP tool/function name to call.")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="JSON-serializable tool arguments.")
    constraints: List[str] = Field(default_factory=list)

class ExecuteStepOutput(BaseModel):
    """Feedback from Execute Agent back to Planner."""
    status: Literal["success", "blocked", "failed"]
    result: str = Field(..., description="The natural language or data result of the execution.")
    artifacts: List[str] = Field(default_factory=list, description="Any files or data objects generated.")
    blocked_reason: Optional[str] = Field(None, description="Why the execution was blocked (if applicable).")
    next_recommendation: Optional[str] = Field(None, description="Suggestion for the next action if failed or blocked.")

class StepExecutionResult(BaseModel):
    """Result for one Python-driven plan step execution."""
    step_id: str
    agent: str
    tool_name: str
    status: Literal["success", "blocked", "failed"]
    result: str
    raw_result: Optional[Any] = None
    next_step: Optional[str] = None
    error: Optional[str] = None

class PlanExecutionResult(BaseModel):
    """Final summary from the Python plan executor."""
    status: Literal["success", "blocked", "failed"]
    goal: str
    completed_steps: List[str] = Field(default_factory=list)
    results: List[StepExecutionResult] = Field(default_factory=list)
    message: str = ""
