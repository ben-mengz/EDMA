from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from agents import Runner
from openai.types.responses import ResponseTextDeltaEvent

from edma_mcp.client.display_payloads import build_plan_live_payload
from edma_mcp.client.models import PlanReview


@dataclass
class ExecutionCallbacks:
    on_status: Optional[Callable[[str], None]] = None
    on_trace: Optional[Callable[[str], None]] = None
    on_final: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_plan_live: Optional[Callable[[Dict[str, Any]], None]] = None
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
        self.live_step_states: Optional[Dict[str, Dict[str, Any]]] = None

    def bind_runtime(self, agent: Any, session: Any) -> None:
        self.agent = agent
        self.session = session

    def reset(self) -> None:
        self.paused_execution_plan = None
        self.paused_execution_reason = None
        self.paused_execution_state = None
        self.active_execution_plan = None
        self.execution_in_progress = False
        self.live_step_states = None

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
        self.live_step_states = self._initialize_step_states(plan)
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
        pause_reason = self.paused_execution_reason or ""
        pause_state = dict(self.paused_execution_state or {})
        self.paused_execution_plan = None
        self.paused_execution_reason = None
        self.paused_execution_state = None
        self.active_execution_plan = plan
        if self.live_step_states is None:
            self.live_step_states = self._initialize_step_states(plan)
        self._mark_waiting_step_completed_on_resume(plan, self.live_step_states, user_message)
        self._mark_running_step(self.live_step_states, self._next_pending_step(plan, self.live_step_states))
        await self._run_execution(
            plan=plan,
            prompt=self._format_resume_execution_prompt(
                plan,
                user_message,
                pause_reason,
                pause_state,
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
        step_states = self.live_step_states or self._initialize_step_states(plan)
        self.live_step_states = step_states
        self._emit_plan_live(
            callbacks,
            plan,
            step_states,
            overall_status="running",
            overall_message=status_label,
            current_step=None,
        )

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
                    if handoff_status:
                        current_step = self._extract_step_id_from_status(handoff_status)
                        self._mark_running_step(step_states, current_step)
                        self._emit_plan_live(
                            callbacks,
                            plan,
                            step_states,
                            overall_status="running",
                            overall_message=self._summarize_handoff_status(handoff_status) or status_label,
                            current_step=current_step,
                        )
                elif event.type == "agent_updated_stream_event":
                    if callbacks.on_trace:
                        callbacks.on_trace(f"Agent active: {event.new_agent.name}")
                elif event.type == "run_item_stream_event":
                    tool_step = self._step_id_from_run_item(plan, step_states, event)
                    if tool_step:
                        self._mark_running_step(step_states, tool_step)
                        self._emit_plan_live(
                            callbacks,
                            plan,
                            step_states,
                            overall_status="running",
                            overall_message=f"Running step {tool_step}",
                            current_step=tool_step,
                        )
                    trace_message = trace_formatter(event)
                    if trace_message and callbacks.on_trace:
                        callbacks.on_trace(trace_message)
        except Exception as exc:
            if callbacks.on_delete_tool_calling_message:
                callbacks.on_delete_tool_calling_message()
            self.execution_in_progress = False
            self.active_execution_plan = None
            self._emit_plan_live(
                callbacks,
                plan,
                step_states,
                overall_status="failed",
                overall_message=f"Plan execution failed: {exc}",
                current_step=self._current_running_step(step_states),
            )
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
        current_step = self._current_running_step(step_states)
        if self.paused_execution_plan is plan:
            blocked_step = self._blocked_step_for_pause(plan, step_states, current_step)
            if current_step and blocked_step and blocked_step != current_step:
                self._mark_completed_step(step_states, current_step, "")
            self._mark_waiting_step(step_states, blocked_step or current_step, final_text)
            self._emit_plan_live(
                callbacks,
                plan,
                step_states,
                overall_status="paused",
                overall_message=final_text or "Execution is waiting for user input.",
                current_step=blocked_step or current_step,
            )
        else:
            self._mark_completed_step(step_states, current_step, final_text)
            self._emit_plan_live(
                callbacks,
                plan,
                step_states,
                overall_status="completed",
                overall_message=final_text or "Execution finished.",
                current_step=None,
            )
            self.live_step_states = step_states
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
        self.live_step_states = None

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
            "the exact step_id, skill, tool_name, arguments, resource_hints, required_inputs, expected_output, on_success, and on_failure.\n"
            "If resource_hints is non-empty, keep those exact resource paths in the handoff. For code-writing, code-generation, "
            "or code-running steps, the specialist must read those skill resources before producing or executing code.\n"
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
            "For the blocked step and all following steps, preserve the plan's exact skill and exact resource_hints values. "
            "If a step is code-writing, code-generation, or code-running and has resource_hints, the specialist must read those "
            "skill resources before producing or executing code.\n"
            "After the blocked step succeeds, continue to the next specialist step via on_success.\n"
            "Never stop at the current specialist if the next agent/tool is available.\n\n"
            f"Structured paused tool status:\n{json.dumps(pause_state, ensure_ascii=False)}\n\n"
            f"Paused execution reason:\n{pause_reason}\n\n"
            f"Latest user reply:\n{user_message}\n\n"
            f"{plan.model_dump_json()}"
        )

    def _initialize_step_states(self, plan: PlanReview) -> Dict[str, Dict[str, Any]]:
        return {
            str(step.step_id): {"status": "pending", "result": ""}
            for step in list(getattr(plan, "steps", []) or [])
        }

    def _current_running_step(self, step_states: Dict[str, Dict[str, Any]]) -> Optional[str]:
        for step_id, state in step_states.items():
            if state.get("status") == "running":
                return step_id
        return None

    def _extract_step_id_from_status(self, text: str) -> Optional[str]:
        text_value = str(text or "")
        match = re.search(r"(?mi)^step_id:\s*([^\s]+)", text_value)
        if match:
            return match.group(1).strip()
        match = re.search(r"(?i)\bexecuting\s+planreview\s+step\s+([^\s.:]+)", text_value)
        if match:
            return match.group(1).strip()
        match = re.search(r"(?i)\brunning\s+step\s+([^\s.:]+)", text_value)
        if match:
            return match.group(1).strip()
        return None

    def _tool_name_from_item(self, item: Any) -> str:
        raw_item = getattr(item, "raw_item", None)
        return (
            getattr(raw_item, "name", None)
            or getattr(raw_item, "type", None)
            or getattr(item, "title", None)
            or getattr(item, "type", "")
            or ""
        )

    def _normalize_tool_name(self, tool_name: str) -> str:
        value = str(tool_name or "").strip()
        if "__" in value:
            value = value.split("__")[-1]
        if "." in value:
            value = value.split(".")[-1]
        return value

    def _summarize_handoff_status(self, text: str) -> str:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            return ""
        head = lines[0]
        if head.lower().startswith("handoff to "):
            step_id = self._extract_step_id_from_status(text)
            if step_id:
                return f"Running step {step_id}"
        return head

    def _mark_running_step(self, step_states: Dict[str, Dict[str, Any]], current_step: Optional[str]) -> None:
        if not current_step:
            return
        for step_id, state in step_states.items():
            if step_id == current_step:
                state["status"] = "running"
                state["result"] = state.get("result", "") or "Running."
            elif state.get("status") == "running":
                state["status"] = "completed"
                state["result"] = state.get("result", "") or "Completed."

    def _mark_waiting_step(self, step_states: Dict[str, Dict[str, Any]], current_step: Optional[str], result_text: str) -> None:
        if not current_step:
            return
        state = step_states.setdefault(str(current_step), {"status": "pending", "result": ""})
        state["status"] = "waiting"
        state["result"] = result_text or state.get("result", "")

    def _mark_completed_step(self, step_states: Dict[str, Dict[str, Any]], current_step: Optional[str], result_text: str) -> None:
        if not current_step:
            return
        state = step_states.setdefault(str(current_step), {"status": "pending", "result": ""})
        state["status"] = "completed"
        state["result"] = result_text or state.get("result", "") or "Completed."

    def _step_id_from_run_item(
        self,
        plan: PlanReview,
        step_states: Dict[str, Dict[str, Any]],
        event: Any,
    ) -> Optional[str]:
        name = getattr(event, "name", "")
        item = getattr(event, "item", None)
        item_type = getattr(item, "type", "")
        if name != "tool_called" and item_type != "tool_call_item":
            return None
        called_tool = self._normalize_tool_name(self._tool_name_from_item(item))
        if not called_tool:
            return None
        for step in list(getattr(plan, "steps", []) or []):
            step_id = str(step.step_id)
            step_state = step_states.get(step_id, {})
            if step_state.get("status") not in {"pending", "running", "waiting"}:
                continue
            if self._normalize_tool_name(getattr(step, "tool_name", "")) == called_tool:
                return step_id
        return None

    def _mark_waiting_step_completed_on_resume(
        self,
        plan: PlanReview,
        step_states: Dict[str, Dict[str, Any]],
        user_message: str,
    ) -> None:
        for state in step_states.values():
            if state.get("status") == "waiting":
                state["status"] = "completed"
                reply = " ".join(str(user_message or "").split())
                state["result"] = f"User input received: {reply}" if reply else "User input received."
                return
        for step in list(getattr(plan, "steps", []) or []):
            step_id = str(step.step_id)
            state = step_states.get(step_id, {})
            if state.get("status") == "pending" and list(getattr(step, "required_inputs", []) or []):
                state["status"] = "completed"
                reply = " ".join(str(user_message or "").split())
                state["result"] = f"User input received: {reply}" if reply else "User input received."
                return

    def _next_pending_step(self, plan: PlanReview, step_states: Dict[str, Dict[str, Any]]) -> Optional[str]:
        for step in list(getattr(plan, "steps", []) or []):
            step_id = str(step.step_id)
            if step_states.get(step_id, {}).get("status") == "pending":
                return step_id
        return None

    def _blocked_step_for_pause(
        self,
        plan: PlanReview,
        step_states: Dict[str, Dict[str, Any]],
        current_step: Optional[str],
    ) -> Optional[str]:
        if current_step:
            step_by_id = {str(step.step_id): step for step in list(getattr(plan, "steps", []) or [])}
            current = step_by_id.get(str(current_step))
            if current:
                next_ref = str(getattr(current, "on_success", "") or "")
                if next_ref and next_ref not in {"done", "stop", "ask_user"}:
                    next_step = step_by_id.get(next_ref)
                    if next_step and list(getattr(next_step, "required_inputs", []) or []):
                        return next_ref
        for step in list(getattr(plan, "steps", []) or []):
            step_id = str(step.step_id)
            state = step_states.get(step_id, {})
            if state.get("status") == "pending" and list(getattr(step, "required_inputs", []) or []):
                return step_id
        return current_step

    def _emit_plan_live(
        self,
        callbacks: ExecutionCallbacks,
        plan: PlanReview,
        step_states: Dict[str, Dict[str, Any]],
        *,
        overall_status: str,
        overall_message: str,
        current_step: Optional[str],
    ) -> None:
        if callbacks.on_plan_live is None:
            return
        callbacks.on_plan_live(
            build_plan_live_payload(
                plan,
                queued_for_approval=False,
                overall_status=overall_status,
                overall_message=overall_message,
                current_step=current_step,
                step_states=step_states,
            )
        )
