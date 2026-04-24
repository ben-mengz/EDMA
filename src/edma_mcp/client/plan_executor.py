from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from edma_mcp.client.models import PlanExecutionResult, PlanReview, PlanStep, StepExecutionResult


class PlanExecutor:
    """Execute an approved PlanReview through direct MCP tool calls."""

    def __init__(
        self,
        bridge: Any,
        on_step_start: Optional[Callable[[PlanStep], Awaitable[None] | None]] = None,
        on_step_result: Optional[Callable[[StepExecutionResult], Awaitable[None] | None]] = None,
    ) -> None:
        self.bridge = bridge
        self.on_step_start = on_step_start
        self.on_step_result = on_step_result

    async def execute(self, plan: PlanReview) -> PlanExecutionResult:
        available_agents = set(await self.bridge.list_agents())
        validation_error = await self._validate_plan(plan, available_agents)
        if validation_error:
            return PlanExecutionResult(
                status="blocked",
                goal=plan.goal,
                message=validation_error,
            )

        step_by_id = {step.step_id: step for step in plan.steps}
        current_id = plan.steps[0].step_id
        completed: List[str] = []
        results: List[StepExecutionResult] = []
        visited: Set[str] = set()
        context: Dict[str, Any] = {}

        while current_id not in {"done", "stop", "ask_user"}:
            if current_id in visited:
                return PlanExecutionResult(
                    status="blocked",
                    goal=plan.goal,
                    completed_steps=completed,
                    results=results,
                    message=f"Execution stopped because step '{current_id}' would repeat.",
                )
            visited.add(current_id)

            step = step_by_id.get(current_id)
            if step is None:
                return PlanExecutionResult(
                    status="blocked",
                    goal=plan.goal,
                    completed_steps=completed,
                    results=results,
                    message=f"Execution stopped because step '{current_id}' does not exist.",
                )

            await self._notify(self.on_step_start, step)
            step_result = await self._execute_step(step, context)
            await self._notify(self.on_step_result, step_result)
            results.append(step_result)

            if step_result.status == "success":
                completed.append(step.step_id)
                self._record_step_context(context, step, step_result.raw_result)
                current_id = step.on_success
            else:
                current_id = step.on_failure
                if current_id == "ask_user":
                    return PlanExecutionResult(
                        status="blocked",
                        goal=plan.goal,
                        completed_steps=completed,
                        results=results,
                        message=f"Step '{step.step_id}' needs user input before continuing: {step_result.result}",
                    )
                if current_id == "stop":
                    return PlanExecutionResult(
                        status=step_result.status,
                        goal=plan.goal,
                        completed_steps=completed,
                        results=results,
                        message=f"Execution stopped after step '{step.step_id}': {step_result.result}",
                    )

        status = "success" if current_id == "done" else "blocked"
        return PlanExecutionResult(
            status=status,
            goal=plan.goal,
            completed_steps=completed,
            results=results,
            message="Plan execution completed." if status == "success" else "Plan execution stopped.",
        )

    async def _validate_plan(self, plan: PlanReview, available_agents: Set[str]) -> str:
        tools_by_agent: Dict[str, Set[str]] = {}
        for step in plan.steps:
            if step.agent not in available_agents:
                return f"Step '{step.step_id}' references unavailable agent '{step.agent}'."
            if not isinstance(step.arguments, dict):
                return f"Step '{step.step_id}' arguments must be a JSON object."
            if step.agent not in tools_by_agent:
                tools = await self.bridge.list_tools(step.agent)
                tools_by_agent[step.agent] = {self._tool_name(tool) for tool in tools if self._tool_name(tool)}
            normalized_tool_name = self._normalize_step_tool_name(step.agent, step.tool_name)
            if normalized_tool_name not in tools_by_agent[step.agent]:
                return (
                    f"Step '{step.step_id}' references unavailable tool "
                    f"'{step.agent}.{normalized_tool_name}'."
                )
        return ""

    async def _execute_step(self, step: PlanStep, context: Dict[str, Any]) -> StepExecutionResult:
        arguments, unresolved_refs = self._resolve_arguments(step.arguments, context)
        missing_inputs = self._missing_inputs_for_step(step, unresolved_refs)
        if missing_inputs:
            return StepExecutionResult(
                step_id=step.step_id,
                agent=step.agent,
                tool_name=step.tool_name,
                status="blocked",
                result="Missing inputs for this step: " + "; ".join(missing_inputs),
                next_step="ask_user",
            )
        try:
            normalized_tool_name = self._normalize_step_tool_name(step.agent, step.tool_name)
            raw_result = await self.bridge.call_tool(step.agent, normalized_tool_name, arguments)
            result_text = self._stringify_tool_result(raw_result)
            return StepExecutionResult(
                step_id=step.step_id,
                agent=step.agent,
                tool_name=normalized_tool_name,
                status="success",
                result=result_text,
                raw_result=raw_result,
                next_step=step.on_success,
            )
        except Exception as exc:
            normalized_tool_name = self._normalize_step_tool_name(step.agent, step.tool_name)
            return StepExecutionResult(
                step_id=step.step_id,
                agent=step.agent,
                tool_name=normalized_tool_name,
                status="failed",
                result=str(exc),
                next_step=step.on_failure,
                error=repr(exc),
            )

    async def _notify(self, callback: Any, payload: Any) -> None:
        if callback is None:
            return
        result = callback(payload)
        if hasattr(result, "__await__"):
            await result

    def _missing_inputs_for_step(self, step: PlanStep, unresolved_refs: Optional[List[str]] = None) -> List[str]:
        missing = list(step.required_inputs or [])
        for placeholder in unresolved_refs or []:
            label = f"Value for {{{{{placeholder}}}}}"
            if label not in missing:
                missing.append(label)
        return missing

    def _resolve_arguments(self, value: Any, context: Dict[str, Any]) -> tuple[Any, List[str]]:
        unresolved: List[str] = []

        def resolve(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if not isinstance(value, str):
                return value

            full_match = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value)
            if full_match:
                ref = full_match.group(1)
                resolved, ok = self._resolve_reference(ref, context)
                if ok:
                    return resolved
                unresolved.append(ref)
                return value

            def replace_match(match: re.Match[str]) -> str:
                ref = match.group(1)
                resolved, ok = self._resolve_reference(ref, context)
                if ok:
                    return str(resolved)
                unresolved.append(ref)
                return match.group(0)

            return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace_match, value)

        return resolve(value), sorted(set(unresolved))

    def _resolve_reference(self, ref: str, context: Dict[str, Any]) -> tuple[Any, bool]:
        parts = [part.strip() for part in ref.split(".") if part.strip()]
        if not parts:
            return None, False
        current = context.get(parts[0])
        if current is None:
            return None, False
        for part in parts[1:]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return None, False
        return current, True

    def _record_step_context(self, context: Dict[str, Any], step: PlanStep, raw_result: Any) -> None:
        value = self._normalize_context_value(raw_result)
        context[step.step_id] = value
        context[self._normalize_step_tool_name(step.agent, step.tool_name)] = value
        if step.tool_name == "calculate_scan_parameters":
            context["calc"] = value

    def _normalize_step_tool_name(self, agent_name: str, tool_name: str) -> str:
        raw = str(tool_name or "").strip()
        prefix = f"{agent_name}."
        if raw.startswith(prefix):
            return raw[len(prefix):]
        return raw

    def _normalize_context_value(self, value: Any) -> Any:
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        text = getattr(value, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except Exception:
                return text
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    def _stringify_plain(self, value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(f"{self._stringify_plain(k)} {self._stringify_plain(v)}" for k, v in value.items())
        if isinstance(value, list):
            return " ".join(self._stringify_plain(item) for item in value)
        return str(value)

    def _tool_name(self, tool: Any) -> str:
        if isinstance(tool, dict):
            return str(tool.get("name") or "")
        return str(getattr(tool, "name", "") or "")

    def _stringify_tool_result(self, result: Any) -> str:
        if isinstance(result, list):
            parts = []
            for item in result:
                text = getattr(item, "text", None)
                parts.append(text if isinstance(text, str) else str(item))
            return "\n".join(parts)
        text = getattr(result, "text", None)
        if isinstance(text, str):
            return text
        return str(result)
