from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agents import Agent, Runner
from openai.types.responses import ResponseTextDeltaEvent

from edma_mcp.client.models import PlanReview
from edma_mcp.client.plan_judge import judge_plan_review


@dataclass
class PlanningCallbacks:
    on_status: Optional[Callable[[str], None]] = None
    on_trace: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None


@dataclass
class PlanningRunResult:
    plan: Optional[PlanReview] = None
    queued_for_approval: bool = False
    error_message: Optional[str] = None


@dataclass
class PendingPlanDecisionResult:
    action: str = "wait"
    plan: Optional[PlanReview] = None
    message: str = ""


class PlanningCoordinator:
    def __init__(self) -> None:
        self.triage_agent = None
        self.planner_agent = None
        self.session = None
        self.bridge = None
        self.pending_plan: Optional[PlanReview] = None

    def bind_runtime(self, triage_agent: Any, planner_agent: Any, session: Any, bridge: Any) -> None:
        self.triage_agent = triage_agent
        self.planner_agent = planner_agent
        self.session = session
        self.bridge = bridge

    def reset(self) -> None:
        self.pending_plan = None

    def has_pending_plan(self) -> bool:
        return self.pending_plan is not None

    def get_pending_plan(self) -> Optional[PlanReview]:
        return self.pending_plan

    def take_pending_plan(self) -> Optional[PlanReview]:
        plan = self.pending_plan
        self.pending_plan = None
        return plan

    async def start_planning(
        self,
        message: str,
        callbacks: PlanningCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        plan_extractor: Callable[[Any], Optional[PlanReview]],
    ) -> PlanningRunResult:
        prompt = self._format_direct_planner_prompt(message)
        return await self._run_planning_cycle(
            original_message=message,
            planner_prompt=prompt,
            live_label="Planning with Planner...",
            callbacks=callbacks,
            trace_formatter=trace_formatter,
            plan_extractor=plan_extractor,
        )

    async def revise_pending_plan(
        self,
        message: str,
        callbacks: PlanningCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        plan_extractor: Callable[[Any], Optional[PlanReview]],
    ) -> PlanningRunResult:
        plan = self.pending_plan
        if not plan:
            return PlanningRunResult(error_message="No pending plan to revise.")
        prompt = self._format_direct_planner_revise_prompt(message, plan)
        return await self._run_planning_cycle(
            original_message=message,
            planner_prompt=prompt,
            live_label="Planning with Planner...",
            callbacks=callbacks,
            trace_formatter=trace_formatter,
            plan_extractor=plan_extractor,
        )

    async def handle_pending_plan_message(
        self,
        message: str,
        callbacks: PlanningCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        plan_extractor: Callable[[Any], Optional[PlanReview]],
        is_approval_message: Callable[[str], bool],
        should_arbitrate_pending_plan: Callable[[str], bool],
    ) -> PendingPlanDecisionResult:
        plan = self.pending_plan
        if not plan:
            return PendingPlanDecisionResult(action="wait", message="No pending plan to review.")

        try:
            decision = await self._classify_pending_plan_message_async(message, plan)
        except Exception:
            if is_approval_message(message):
                decision = "approve"
            elif should_arbitrate_pending_plan(message):
                decision = "revise"
            else:
                decision = "wait"

        if decision == "approve":
            plan = self.take_pending_plan()
            return PendingPlanDecisionResult(action="approve", plan=plan)
        if decision == "revise":
            result = await self.revise_pending_plan(
                message,
                callbacks=callbacks,
                trace_formatter=trace_formatter,
                plan_extractor=plan_extractor,
            )
            if result.error_message:
                return PendingPlanDecisionResult(action="wait", message=result.error_message)
            return PendingPlanDecisionResult(
                action="revise",
                plan=result.plan,
                message=(
                    "Plan is ready for approval again."
                    if result.queued_for_approval
                    else "Plan revision completed but was not queued for approval."
                ),
            )
        return PendingPlanDecisionResult(
            action="wait",
            message="Plan is still waiting for approval. Say 'approve', 'go', or 'go ahead' to execute it, or tell me what to change.",
        )

    async def _run_planning_cycle(
        self,
        *,
        original_message: str,
        planner_prompt: str,
        live_label: str,
        callbacks: PlanningCallbacks,
        trace_formatter: Callable[[Any], Optional[str]],
        plan_extractor: Callable[[Any], Optional[PlanReview]],
    ) -> PlanningRunResult:
        if self.planner_agent is None or self.session is None:
            return PlanningRunResult(error_message="Planner runtime is not ready.")

        final_text = await self._run_agent_to_text(
            planner_prompt,
            live_label=live_label,
            trace_formatter=trace_formatter,
            callbacks=callbacks,
        )
        plan = plan_extractor(final_text)
        if not plan:
            self.pending_plan = None
            return PlanningRunResult(error_message=final_text or "Planner did not return a valid PlanReview.")

        replan_history = []
        max_auto_replans = 5
        for attempt_index in range(max_auto_replans + 1):
            try:
                plan.judge_report = await judge_plan_review(plan, self.bridge)
            except Exception as exc:
                plan.judge_report = None
                if callbacks.on_trace:
                    callbacks.on_trace(f"Judge failed: {exc}")
                break

            judge_report = getattr(plan, "judge_report", None)
            judge_status = getattr(judge_report, "status", "pass")
            if judge_status == "pass":
                break
            fixable = self._is_fixable_judge_report(judge_report)
            if attempt_index >= max_auto_replans or not fixable:
                if not fixable and judge_report is not None and callbacks.on_trace:
                    categories = sorted({
                        str(getattr(issue, "category", "unknown"))
                        for issue in list(getattr(judge_report, "issues", []) or [])
                        if getattr(issue, "severity", "") == "blocking"
                    })
                    callbacks.on_trace(
                        "Judge classified this plan as non-fixable for auto-replan: "
                        f"grounding={getattr(judge_report, 'grounding_status', 'unknown')}, "
                        f"blocking_categories={categories}"
                    )
                break

            replan_history.append(
                f"Blocked {attempt_index + 1} time(s): {getattr(judge_report, 'summary', 'Judge requested revision')}."
            )
            reason_brief = self._judge_reason_brief(judge_report)
            if callbacks.on_trace:
                callbacks.on_trace(
                    f"Judge requested auto-replan #{attempt_index + 1}: "
                    f"{getattr(judge_report, 'grounding_status', 'unknown')} / {judge_status} / {reason_brief}"
                )
            replan_prompt = self._format_judge_replan_prompt(original_message, plan, judge_report, attempt_index + 1)
            final_text = await self._run_agent_to_text(
                replan_prompt,
                live_label=f"Auto-replan #{attempt_index + 1}... {reason_brief}",
                trace_formatter=trace_formatter,
                callbacks=callbacks,
            )
            replanned = plan_extractor(final_text)
            if not replanned:
                break
            plan = replanned

        plan.judge_replan_history = replan_history
        judge_status = getattr(getattr(plan, "judge_report", None), "status", "pass")
        self.pending_plan = plan if judge_status == "pass" else None
        return PlanningRunResult(
            plan=plan,
            queued_for_approval=judge_status == "pass",
        )

    async def _run_agent_to_text(
        self,
        message: str,
        *,
        live_label: str,
        trace_formatter: Callable[[Any], Optional[str]],
        callbacks: PlanningCallbacks,
    ) -> str:
        if self.bridge is not None and hasattr(self.bridge, "planner_discovery_log"):
            self.bridge.planner_discovery_log = []
        response = Runner.run_streamed(self.planner_agent, message, session=self.session)
        bot_text = []
        live_items = []
        planner_poll_active = True

        async def poll_planner_discovery():
            last_log = []
            while planner_poll_active:
                try:
                    current = list(getattr(self.bridge, "planner_discovery_log", []) or [])
                except Exception:
                    current = []
                if current and current != last_log:
                    formatted = [self._format_planner_discovery_entry(item) for item in current[-3:]]
                    for status in formatted:
                        if not live_items or live_items[-1] != status:
                            live_items.append(status)
                    if callbacks.on_status:
                        callbacks.on_status(self._compose_live_planning_status(live_label, live_items))
                    last_log = current
                await asyncio.sleep(0.2)

        import asyncio

        planner_poll_task = asyncio.create_task(poll_planner_discovery())
        async for event in response.stream_events():
            if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                bot_text.append(event.data.delta)
            elif event.type == "agent_updated_stream_event":
                status = f"Agent active: {event.new_agent.name}"
                if not live_items or live_items[-1] != status:
                    live_items.append(status)
                    if callbacks.on_status:
                        callbacks.on_status(self._compose_live_planning_status(live_label, live_items))
            elif event.type == "run_item_stream_event":
                trace_message = trace_formatter(event)
                if trace_message:
                    if not live_items or live_items[-1] != trace_message:
                        live_items.append(trace_message)
                        if callbacks.on_status:
                            callbacks.on_status(self._compose_live_planning_status(live_label, live_items))
        planner_poll_active = False
        try:
            await planner_poll_task
        except Exception:
            pass
        return "".join(bot_text)

    async def _classify_pending_plan_message_async(self, message: str, plan: PlanReview) -> str:
        classifier_agent = self._build_pending_plan_gate_agent()
        prompt = self._format_pending_plan_gate_prompt(message, plan)
        result = await Runner.run(starting_agent=classifier_agent, input=prompt)
        decision = self._extract_pending_plan_gate_decision(getattr(result, "final_output", result))
        return decision or "wait"

    def _build_pending_plan_gate_agent(self):
        kwargs = {
            "name": "PendingPlanGate",
            "instructions": (
                "You classify whether the latest user reply should APPROVE execution of a pending plan, "
                "REVISE the plan, or WAIT for clearer instruction.\n"
                "Return JSON only with keys decision and rationale.\n"
                "decision must be one of: approve, revise, wait.\n"
                "Approve when the user clearly wants execution now, even in natural language like "
                "\"looks good, go ahead\" or \"sounds fine, let's do it\".\n"
                "Revise when the user changes requirements, asks to modify the plan, asks a plan question, "
                "or introduces new constraints.\n"
                "Wait when the user is only acknowledging, praising, or being vague without clear approval or revision.\n"
                "\"looks good\" alone should be wait.\n"
                "\"looks good, go ahead\" should be approve.\n"
                "Do not call tools. Do not include markdown."
            ),
        }
        model = getattr(self.triage_agent, "model", None)
        if model is not None:
            kwargs["model"] = model
        model_settings = getattr(self.triage_agent, "model_settings", None)
        if model_settings is not None:
            kwargs["model_settings"] = model_settings
        return Agent(**kwargs)

    def _extract_pending_plan_gate_decision(self, output: Any) -> Optional[str]:
        if hasattr(output, "model_dump"):
            output = output.model_dump()
        if isinstance(output, dict):
            decision = str(output.get("decision", "")).strip().lower()
            return decision if decision in {"approve", "revise", "wait"} else None
        if not isinstance(output, str):
            return None
        text = output.strip()
        candidates = [text]
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            decision = str(data.get("decision", "")).strip().lower()
            if decision in {"approve", "revise", "wait"}:
                return decision
        return None

    def _is_fixable_judge_report(self, report: Any) -> bool:
        if report is None:
            return False
        if getattr(report, "grounding_status", "") == "ungrounded":
            return False
        issues = list(getattr(report, "issues", []) or [])
        if not issues:
            return False
        fixable_categories = {"order", "parameter", "grounding"}
        for issue in issues:
            if getattr(issue, "severity", "") != "blocking":
                continue
            if getattr(issue, "category", "") not in fixable_categories:
                return False
        return True

    def _judge_reason_brief(self, report: Any) -> str:
        if report is None:
            return "unknown reason"
        issues = list(getattr(report, "issues", []) or [])
        if issues:
            first = issues[0]
            category = str(getattr(first, "category", "issue"))
            message = str(getattr(first, "message", "")).strip()
            if message:
                return f"{category}: {message}"
            return category
        summary = str(getattr(report, "summary", "")).strip()
        return summary or "judge requested revision"

    def _format_judge_replan_prompt(self, original_message: str, plan: PlanReview, report: Any, attempt_index: int) -> str:
        issue_lines = []
        for issue in list(getattr(report, "issues", []) or [])[:12]:
            prefix = f"step {getattr(issue, 'step_id', 'plan')}" if getattr(issue, "step_id", None) else "plan"
            issue_lines.append(f"- {prefix}: {getattr(issue, 'category', 'issue')} - {getattr(issue, 'message', '')}")
        issue_block = "\n".join(issue_lines) if issue_lines else "- No detailed issues were provided."
        return (
            "The previous PlanReview failed deterministic contract validation and must be revised.\n"
            "You are the Planner. Return a revised PlanReview only. Do not execute.\n"
            "Revise the workflow so it remains grounded in registered skills and satisfies the skill contract order and MCP tool schema.\n"
            "Do not keep invalid step ordering or invalid parameters.\n"
            f"Auto-replan attempt: {attempt_index}\n\n"
            f"Original user message:\n{original_message}\n\n"
            f"Previous PlanReview:\n{plan.model_dump_json()}\n\n"
            f"Judge summary:\n{getattr(report, 'summary', '')}\n"
            f"Judge issues:\n{issue_block}"
        )

    def _compose_live_planning_status(self, base_label: str, items: list[str]) -> str:
        lines = [base_label]
        for item in items[-4:]:
            if item:
                lines.append(item)
        return "\n".join(lines)

    def _format_planner_discovery_entry(self, entry: Any) -> str:
        text = str(entry or "").strip()
        if text == "list_skills":
            return "Planner: listed skills"
        if text == "list_agents_capabilities":
            return "Planner: checked agent capabilities"
        if text.startswith("get_skill_manifest:"):
            return f"Planner: read manifest {text.split(':', 1)[1]}"
        if text.startswith("read_skill_content:"):
            return f"Planner: read skill {text.split(':', 1)[1]}"
        if text.startswith("list_skill_resources:"):
            return f"Planner: listed resources for {text.split(':', 1)[1]}"
        if text.startswith("read_skill_resource:"):
            _, rest = text.split(":", 1)
            return f"Planner: read resource {rest}"
        return f"Planner: {text}"

    def _format_direct_planner_prompt(self, message: str) -> str:
        return (
            "The user provided a new requirement. You are the Planner.\n"
            "Read the relevant registered skills/playbooks and return a PlanReview for user approval.\n"
            "Do not execute tools except the planner discovery tools needed to read skills and validate capabilities.\n\n"
            "Context arbitration:\n"
            "- Treat the latest user message below as the authoritative planning goal.\n"
            "- Previous conversation may contain an approved plan, a blocked execution step, or missing-input request. Do not continue that prior step unless the latest user message explicitly answers that step, says continue/resume, or asks to execute/acquire/reconstruct now.\n"
            "- If the latest user message asks a separate tool/skill task, create a fresh PlanReview from step_id 1 and choose skills only from that latest request.\n\n"
            f"Latest user message:\n{message}"
        )

    def _format_direct_planner_revise_prompt(self, message: str, plan: PlanReview) -> str:
        return (
            "The user sent a message while a PlanReview is pending.\n"
            "You are the Planner. Revise or reset the pending plan based on the latest user message.\n"
            "Return a new PlanReview only. Do not execute.\n\n"
            "Decision rules:\n"
            "- RESET: If the latest user message is a new goal, a new task, a separate question that needs tools/skills, or asks for something not clearly framed as an edit to the pending plan, ignore the pending plan as execution context and create a fresh PlanReview from step_id 1.\n"
            "- REVISE: Only revise/continue the pending plan if the latest user message explicitly refers to that plan, says to change one of its steps/arguments, says continue/resume, or provides information requested by a pending step.\n"
            "- The previous plan is optional context only. Do not continue step numbering from it after RESET.\n\n"
            f"Latest user message:\n{message}\n\n"
            f"Pending PlanReview optional context:\n{plan.model_dump_json()}"
        )

    def _format_pending_plan_gate_prompt(self, message: str, plan: PlanReview) -> str:
        return (
            "Classify the user's latest reply about a pending plan.\n\n"
            f"Pending plan JSON:\n{plan.model_dump_json()}\n\n"
            f"Latest user message:\n{message}\n\n"
            "Return JSON only, for example:\n"
            '{"decision":"approve","rationale":"The user clearly authorized execution."}'
        )
