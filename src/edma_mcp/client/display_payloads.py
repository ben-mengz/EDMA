from __future__ import annotations

from typing import Any, Dict, List, Optional

from edma_mcp.client.models import PlanReview


def build_chat_message_payload(text: str, role: str = "bot") -> Dict[str, Any]:
    return {
        "kind": "chat_message",
        "role": role,
        "text": str(text or ""),
    }


def build_plan_review_payload(
    plan: Optional[PlanReview],
    *,
    discovery_log: Optional[List[str]] = None,
    queued_for_approval: bool = False,
    error_message: Optional[str] = None,
) -> Dict[str, Any]:
    if plan is None:
        return {
            "kind": "plan_review",
            "title": "Plan review required",
            "goal": "",
            "summary": "",
            "table": {
                "columns": ["#", "What will happen", "Ask at step", "How it will be done", "Next"],
                "rows": [],
            },
            "sections": [],
            "queued_for_approval": False,
            "error_message": error_message,
            "action_hint": "",
        }
    user_plan = plan.to_user_plan() if hasattr(plan, "to_user_plan") else {}
    judge_report = getattr(plan, "judge_report", None)
    steps = list(user_plan.get("steps", []))
    table_rows = [
        {
            "step_id": str(step.get("step_id", "")),
            "action": str(step.get("action", "")),
            "required_inputs": list(step.get("required_inputs", []) or []),
            "arguments": list(step.get("arguments", []) or []),
            "next_step": str(step.get("next_step", "")),
        }
        for step in steps
    ]
    sections: List[Dict[str, Any]] = []
    if judge_report:
        sections.append(
            {
                "title": "Judge",
                "style": "warning",
                "facts": [
                    {"label": "Judge status", "value": getattr(judge_report, "status", None)},
                    {"label": "Grounding", "value": getattr(judge_report, "grounding_status", None)},
                    {"label": "Summary", "value": getattr(judge_report, "summary", None)},
                ],
                "items": [
                    f"[{getattr(item, 'severity', 'info')}] {getattr(item, 'step_id', None) or 'plan'}: {getattr(item, 'message', '')}"
                    for item in list(getattr(judge_report, "issues", []) or [])[:12]
                ],
            }
        )
    replan_items = list(getattr(plan, "judge_replan_history", []) or [])
    if replan_items:
        sections.append(
            {
                "title": "Judge replan history",
                "style": "muted",
                "items": [str(item) for item in replan_items],
            }
        )
    discovery_items = list(discovery_log or [])
    if discovery_items:
        sections.append(
            {
                "title": "Planner discovery",
                "style": "muted",
                "items": [str(item) for item in discovery_items],
            }
        )
    risk_items = list(getattr(plan, "risks", []) or [])
    if risk_items:
        sections.append(
            {
                "title": "Risks / assumptions",
                "style": "default",
                "items": [str(item) for item in risk_items],
            }
        )
    return {
        "kind": "plan_review",
        "title": "Plan review required",
        "goal": plan.goal,
        "summary": user_plan.get("summary", plan.summary),
        "table": {
            "columns": ["#", "What will happen", "Ask at step", "How it will be done", "Next"],
            "rows": table_rows,
        },
        "sections": sections,
        "queued_for_approval": bool(queued_for_approval),
        "error_message": error_message,
        "action_hint": "Review this plan. Reply approve, go, or go ahead to execute. If a step needs input, execution will pause at that step.",
    }


def build_execution_status_payload(
    *,
    phase: str,
    status_text: Optional[str] = None,
    trace_items: Optional[List[str]] = None,
    current_agent: Optional[str] = None,
    current_step: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": "execution_status",
        "phase": phase,
        "status_text": status_text,
        "trace_items": list(trace_items or []),
        "current_agent": current_agent,
        "current_step": current_step,
    }


def build_execution_result_payload(
    *,
    status: str,
    final_message: str,
    completed_steps: Optional[List[str]] = None,
    result_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "kind": "execution_result",
        "status": status,
        "final_message": final_message,
        "completed_steps": list(completed_steps or []),
        "result_items": list(result_items or []),
    }
