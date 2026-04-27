from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from edma_mcp.client.execution_coordinator import ApprovedPlanExecutionCoordinator, ExecutionCallbacks
from edma_mcp.client.planning_coordinator import (
    PendingPlanDecisionResult,
    PlanningCallbacks,
    PlanningCoordinator,
    PlanningRunResult,
)


@dataclass
class WorkflowRoutingResult:
    action: str
    planning_result: Optional[PlanningRunResult] = None
    message: str = ""


class WorkflowCoordinator:
    def __init__(self) -> None:
        self.planning = PlanningCoordinator()
        self.execution = ApprovedPlanExecutionCoordinator()

    def bind_runtime(self, triage_agent: Any, planner_agent: Any, session: Any, bridge: Any) -> None:
        self.planning.bind_runtime(triage_agent, planner_agent, session, bridge)
        self.execution.bind_runtime(triage_agent, session)

    def reset(self) -> None:
        self.planning.reset()
        self.execution.reset()

    def has_pending_plan(self) -> bool:
        return self.planning.has_pending_plan()

    def has_paused_execution(self) -> bool:
        return self.execution.has_paused_execution()

    def handle_tool_status(self, tool_status: Any) -> None:
        self.execution.handle_tool_status(tool_status)

    def should_intercept_trigger(self, tool_status: Any) -> bool:
        return self.execution.should_intercept_trigger(tool_status)

    async def handle_user_message(
        self,
        message: str,
        *,
        planning_callbacks: PlanningCallbacks,
        execution_callbacks: ExecutionCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        plan_extractor: Callable[[Any], Any],
        handoff_status_extractor: Callable[[str], Optional[str]],
        execution_status_line_checker: Callable[[str], bool],
    ) -> WorkflowRoutingResult:
        if self.execution.has_paused_execution() and not self._is_new_requirement_message(message):
            await self.execution.resume_execution(
                message,
                execution_callbacks,
                trace_formatter=trace_formatter,
                handoff_status_extractor=handoff_status_extractor,
                execution_status_line_checker=execution_status_line_checker,
            )
            return WorkflowRoutingResult(action="handled")

        if self.planning.has_pending_plan() and self._is_approval_message(message):
            plan = self.planning.take_pending_plan()
            if plan is None:
                return WorkflowRoutingResult(action="message", message="No pending plan to approve.")
            await self.execution.execute_plan(
                plan,
                execution_callbacks,
                trace_formatter=trace_formatter,
                handoff_status_extractor=handoff_status_extractor,
                execution_status_line_checker=execution_status_line_checker,
            )
            return WorkflowRoutingResult(action="handled")

        if self.planning.has_pending_plan():
            pending_result = await self.planning.handle_pending_plan_message(
                message,
                callbacks=planning_callbacks,
                trace_formatter=trace_formatter,
                plan_extractor=plan_extractor,
                is_approval_message=self._is_approval_message,
                should_arbitrate_pending_plan=self._should_arbitrate_pending_plan,
            )
            if pending_result.action == "approve":
                plan = pending_result.plan
                if plan is None:
                    return WorkflowRoutingResult(action="message", message="No pending plan to approve.")
                await self.execution.execute_plan(
                    plan,
                    execution_callbacks,
                    trace_formatter=trace_formatter,
                    handoff_status_extractor=handoff_status_extractor,
                    execution_status_line_checker=execution_status_line_checker,
                )
                return WorkflowRoutingResult(action="handled")
            if pending_result.action == "revise":
                return WorkflowRoutingResult(
                    action="plan",
                    planning_result=PlanningRunResult(
                        plan=pending_result.plan,
                        queued_for_approval=self.planning.has_pending_plan(),
                    ),
                )
            return WorkflowRoutingResult(action="message", message=pending_result.message)

        if self._is_new_requirement_message(message):
            planning_result = await self.planning.start_planning(
                message,
                callbacks=planning_callbacks,
                trace_formatter=trace_formatter,
                plan_extractor=plan_extractor,
            )
            return WorkflowRoutingResult(action="plan", planning_result=planning_result)

        return WorkflowRoutingResult(action="chat")

    def _normalize_plan_message(self, message: str) -> str:
        normalized = " ".join(str(message).strip().lower().split())
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", normalized).strip()

    def _is_approval_message(self, message: str) -> bool:
        normalized = self._normalize_plan_message(message)
        if not normalized:
            return False
        negative_phrases = {
            "dont",
            "do not",
            "not yet",
            "not now",
            "wait",
            "hold on",
            "hold off",
            "stop",
            "cancel",
        }
        if any(phrase in normalized for phrase in negative_phrases):
            return False
        approval_phrases = {
            "approve",
            "approved",
            "go",
            "run",
            "yes",
            "ok",
            "okay",
            "do it",
            "proceed",
            "start",
            "start it",
            "continue",
            "confirm",
            "execute",
            "execute it",
            "批准",
            "执行",
            "批准执行",
        }
        if normalized in approval_phrases:
            return True
        approval_patterns = (
            r"\bgo ahead\b",
            r"\bgo for it\b",
            r"\bapprove(?:d)?(?: (?:it|this|that|the plan|the workflow))?\b",
            r"\bproceed(?: with (?:it|this|that|the plan|the workflow))?\b",
            r"\brun (?:it|this|that|the plan|the workflow)\b",
            r"\bexecute(?: (?:it|this|that|the plan|the workflow))?\b",
            r"\bstart(?: (?:it|this|that|the plan|the workflow))?\b",
            r"\bcontinue(?: with (?:it|this|that|the plan|the workflow))?\b",
            r"\bconfirm(?: and (?:run|execute|start))?\b",
            r"\bdo it\b",
            r"批准(?:执行)?",
            r"执行",
        )
        return any(re.search(pattern, normalized) for pattern in approval_patterns)

    def _is_new_requirement_message(self, message: str) -> bool:
        normalized = " ".join(str(message).strip().lower().split())
        planning_keywords = {
            "plan",
            "plan again",
            "replan",
            "plan it again",
            "suggest",
            "recommend",
            "recommendation",
            "workflow",
            "acquire",
            "acquisition",
            "ptychography",
            "4d-stem",
            "defocus",
            "scan step",
            "dose",
            "sample thickness",
            "新需求",
            "重新计划",
            "重新plan",
            "再计划",
            "建议",
            "推荐",
            "采集",
            "扫描",
            "\u65b0\u9700\u6c42",
            "\u91cd\u65b0\u8ba1\u5212",
            "\u91cd\u65b0plan",
            "\u518d\u8ba1\u5212",
            "\u5efa\u8bae",
            "\u63a8\u8350",
            "\u91c7\u96c6",
            "\u626b\u63cf",
        }
        return any(keyword in normalized for keyword in planning_keywords)

    def _should_arbitrate_pending_plan(self, message: str) -> bool:
        normalized = self._normalize_plan_message(message)
        if not normalized:
            return False
        revision_patterns = (
            r"\bchange\b",
            r"\bupdate\b",
            r"\brevise\b",
            r"\bmodify\b",
            r"\badjust\b",
            r"\breplan\b",
            r"\bplan again\b",
            r"\bnew plan\b",
            r"\binstead\b",
            r"\breplace\b",
            r"\bswitch\b",
            r"\bdifferent\b",
            r"\banother\b",
            r"\buse\b",
            r"\bmake it\b",
            r"\bcan you\b",
            r"\bcould you\b",
            r"\bi want\b",
            r"\bi need\b",
            r"\bwhat if\b",
            r"\bwhy\b",
            r"\bhow\b",
            r"\?",
            r"重新",
            r"修改",
            r"调整",
            r"换成",
        )
        return self._is_new_requirement_message(message) or any(re.search(pattern, normalized) for pattern in revision_patterns)
