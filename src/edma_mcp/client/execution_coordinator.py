from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from agents import Runner
from openai.types.responses import ResponseTextDeltaEvent

from edma_mcp.client.models import PlanReview


@dataclass
class ExecutionCallbacks:
    on_status: Optional[Callable[[str], None]] = None
    on_trace: Optional[Callable[[str], None]] = None
    on_final: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_delete_tool_calling_message: Optional[Callable[[], None]] = None
    on_clear_trace_later: Optional[Callable[[], None]] = None


class ApprovedPlanExecutionCoordinator:
    def __init__(self) -> None:
        self.agent = None
        self.session = None
        self.paused_execution_plan: Optional[PlanReview] = None
        self.paused_execution_reason: Optional[str] = None
        self.paused_execution_state: Optional[Dict[str, Any]] = None
        self.active_execution_plan: Optional[PlanReview] = None
        self.execution_in_progress: bool = False

    def bind_runtime(self, agent: Any, session: Any) -> None:
        self.agent = agent
        self.session = session

    def reset(self) -> None:
        self.paused_execution_plan = None
        self.paused_execution_reason = None
        self.paused_execution_state = None
        self.active_execution_plan = None
        self.execution_in_progress = False

    def has_paused_execution(self) -> bool:
        return self.paused_execution_plan is not None

    def should_intercept_trigger(self, tool_status: Any) -> bool:
        return self.execution_in_progress and isinstance(tool_status, dict)

    def handle_tool_status(self, status: Any) -> None:
        if not isinstance(status, dict):
            return
        if not (self.execution_in_progress or self.paused_execution_plan):
            return
        if status.get("needs_user_input"):
            plan = self.active_execution_plan or self.paused_execution_plan
            if plan is not None:
                self.paused_execution_plan = plan
            self.paused_execution_state = {
                "tool_name": status.get("tool_name"),
                "status": status.get("status"),
                "message": status.get("message"),
                "error": status.get("error"),
                "required_inputs": list(status.get("required_inputs") or []),
            }
            self.paused_execution_reason = str(status.get("message") or status.get("error") or "")

    async def execute_plan(
        self,
        plan: PlanReview,
        callbacks: ExecutionCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        handoff_status_extractor: Callable[[str], Optional[str]],
        execution_status_line_checker: Callable[[str], bool],
    ) -> None:
        self.paused_execution_plan = None
        self.paused_execution_reason = None
        self.paused_execution_state = None
        self.active_execution_plan = plan
        await self._run_execution(
            plan=plan,
            prompt=self._format_execution_prompt(plan),
            callbacks=callbacks,
            trace_formatter=trace_formatter,
            handoff_status_extractor=handoff_status_extractor,
            execution_status_line_checker=execution_status_line_checker,
            status_label="Triage executing approved plan...",
        )

    async def resume_execution(
        self,
        user_message: str,
        callbacks: ExecutionCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        handoff_status_extractor: Callable[[str], Optional[str]],
        execution_status_line_checker: Callable[[str], bool],
    ) -> None:
        plan = self.paused_execution_plan
        if not plan:
            if callbacks.on_error:
                callbacks.on_error("No paused execution is waiting to resume.")
            return
        self.active_execution_plan = plan
        await self._run_execution(
            plan=plan,
            prompt=self._format_resume_execution_prompt(
                plan,
                user_message,
                self.paused_execution_reason or "",
                dict(self.paused_execution_state or {}),
            ),
            callbacks=callbacks,
            trace_formatter=trace_formatter,
            handoff_status_extractor=handoff_status_extractor,
            execution_status_line_checker=execution_status_line_checker,
            status_label="Triage resuming paused plan...",
        )

    async def _run_execution(
        self,
        plan: PlanReview,
        prompt: str,
        callbacks: ExecutionCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        handoff_status_extractor: Callable[[str], Optional[str]],
        execution_status_line_checker: Callable[[str], bool],
        status_label: str,
    ) -> None:
        if self.agent is None or self.session is None:
            raise RuntimeError("Execution coordinator runtime is not bound to an agent and session.")

        if callbacks.on_status:
            callbacks.on_status(status_label)

        self.execution_in_progress = True
        response = Runner.run_streamed(self.agent, prompt, session=self.session)
        bot_text = []
        try:
            async for event in response.stream_events():
                if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                    delta = event.data.delta
                    bot_text.append(delta)
                    candidate = "".join(bot_text)
                    handoff_status = handoff_status_extractor(candidate)
                    if handoff_status and callbacks.on_status:
                        callbacks.on_status(handoff_status)
                elif event.type == "agent_updated_stream_event":
                    if callbacks.on_trace:
                        callbacks.on_trace(f"Agent active: {event.new_agent.name}")
                elif event.type == "run_item_stream_event":
                    trace_message = trace_formatter(event)
                    if trace_message and callbacks.on_trace:
                        callbacks.on_trace(trace_message)
        except Exception as exc:
            if callbacks.on_delete_tool_calling_message:
                callbacks.on_delete_tool_calling_message()
            self.execution_in_progress = False
            self.active_execution_plan = None
            if callbacks.on_error:
                callbacks.on_error(f"Plan execution failed: {exc}")
            if callbacks.on_clear_trace_later:
                callbacks.on_clear_trace_later()
            return
        finally:
            self.execution_in_progress = False

        if callbacks.on_delete_tool_calling_message:
            callbacks.on_delete_tool_calling_message()
        final_text = self._strip_execution_status_text("".join(bot_text), execution_status_line_checker).strip()
        self._handle_completion(plan, final_text)
        self.active_execution_plan = None
        if final_text and callbacks.on_final:
            callbacks.on_final(final_text)
        if callbacks.on_clear_trace_later:
            callbacks.on_clear_trace_later()

    def _handle_completion(self, plan: PlanReview, final_text: str) -> None:
        if self.paused_execution_state and self.paused_execution_plan is plan:
            return
        if self._looks_like_missing_input_pause(final_text):
            self.paused_execution_plan = plan
            self.paused_execution_reason = final_text
            return
        self.paused_execution_plan = None
        self.paused_execution_reason = None
        self.paused_execution_state = None

    def _strip_execution_status_text(self, text: str, execution_status_line_checker: Callable[[str], bool]) -> str:
        cleaned = []
        for line in str(text).splitlines():
            if execution_status_line_checker(line):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)

    def _looks_like_missing_input_pause(self, text: str) -> bool:
        normalized = " ".join(str(text or "").split()).lower()
        pause_patterns = (
            r"\bplease provide\b",
            r"\bneeds? (?:one |a )?missing input\b",
            r"\bmissing input\b",
            r"\brequired input\b",
            r"\bbefore continuing\b",
            r"\bi.ll continue\b",
            r"\bcontinue from the\b",
        )
        return any(re.search(pattern, normalized) for pattern in pause_patterns)

    def _format_execution_prompt(self, plan: PlanReview) -> str:
        return (
            "The user approved this PlanReview. You are Triage. Execute it now by specialist handoffs.\n"
            "Do not call create_workflow_plan. Do not re-plan.\n"
            "Read the PlanReview steps in order. For each current step, hand off to step.agent and include "
            "the exact step_id, tool_name, arguments, required_inputs, expected_output, on_success, and on_failure.\n"
            "If the current step has required_inputs, ask the user only for those inputs and stop before calling tools for that step.\n"
            "If a step succeeds, continue to its on_success step. If it fails, follow on_failure.\n"
            "Never say you lack SEM/control access when the relevant specialist handoff/tool is available.\n\n"
            f"{plan.model_dump_json()}"
        )

    def _format_resume_execution_prompt(
        self,
        plan: PlanReview,
        user_message: str,
        pause_reason: str,
        pause_state: Dict[str, Any],
    ) -> str:
        return (
            "The user is replying to a paused approved PlanReview execution.\n"
            "You are Triage. Resume the same approved plan now by specialist handoffs.\n"
            "Do not call create_workflow_plan. Do not re-plan.\n"
            "The previous execution paused because a specialist asked for a missing input.\n"
            "Use the latest user reply below as the answer to that missing-input request, then continue the same approved workflow.\n"
            "After the blocked step succeeds, continue to the next specialist step via on_success.\n"
            "Never stop at the current specialist if the next agent/tool is available.\n\n"
            f"Structured paused tool status:\n{json.dumps(pause_state, ensure_ascii=False)}\n\n"
            f"Paused execution reason:\n{pause_reason}\n\n"
            f"Latest user reply:\n{user_message}\n\n"
            f"{plan.model_dump_json()}"
        )
