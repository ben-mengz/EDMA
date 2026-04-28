from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from edma_mcp.client.models import PlanJudgeIssue, PlanJudgeReport, PlanReview
from edma_mcp.skills.provider import FilesystemSkillProvider


@dataclass
class SkillContract:
    skill_id: str
    required_tools: List[str]
    ordered_tools: List[str]
    resource_paths: List[str]


def _load_skill_contracts(playbooks_dir: str) -> Dict[str, SkillContract]:
    contracts: Dict[str, SkillContract] = {}
    provider = FilesystemSkillProvider(playbooks_dir)
    if not os.path.isdir(playbooks_dir):
        return contracts
    for meta in provider.list_skills():
        skill_id = str(meta.get("id", "")).strip()
        if not skill_id:
            continue
        try:
            manifest = provider.get_skill_manifest(skill_id)
        except Exception:
            continue
        try:
            resources = provider.list_skill_resources(skill_id)
        except Exception:
            resources = []
        required_tools = manifest.get("required_tools") or []
        ordered_tools = manifest.get("ordered_tools") or []
        if not isinstance(required_tools, list):
            required_tools = []
        if not isinstance(ordered_tools, list):
            ordered_tools = []
        contracts[skill_id] = SkillContract(
            skill_id=skill_id,
            required_tools=[str(tool) for tool in required_tools],
            ordered_tools=[str(tool) for tool in ordered_tools],
            resource_paths=[str(resource.get("path")) for resource in resources if isinstance(resource, dict) and str(resource.get("path", "")).strip()],
        )
    return contracts


def _normalize_mcp_tools(mcp_tools: Any) -> List[Dict[str, Any]]:
    if not mcp_tools:
        return []
    if not isinstance(mcp_tools, list):
        return []
    out: List[Dict[str, Any]] = []
    for tool in mcp_tools:
        if isinstance(tool, dict):
            out.append(tool)
            continue
        record: Dict[str, Any] = {}
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        if isinstance(name, str):
            record["name"] = name
        if isinstance(description, str):
            record["description"] = description
        if isinstance(schema, dict):
            record["inputSchema"] = schema
        out.append(record)
    return out


def _is_subsequence(sequence: List[str], ordered: List[str]) -> bool:
    if not sequence:
        return False
    idx = 0
    for item in ordered:
        if idx < len(sequence) and item == sequence[idx]:
            idx += 1
    return idx == len(sequence)


def _normalize_step_tool_name(agent_name: str, tool_name: str) -> Tuple[str, str]:
    raw = str(tool_name or "").strip()
    prefix = f"{agent_name}."
    if raw.startswith(prefix):
        normalized = raw[len(prefix):]
        return normalized, f"{agent_name}.{normalized}"
    return raw, f"{agent_name}.{raw}"


async def judge_plan_review(plan: PlanReview, bridge: Any, playbooks_dir: Optional[str] = None) -> PlanJudgeReport:
    playbooks_root = playbooks_dir
    if not playbooks_root:
        if hasattr(bridge, "_resolve_playbooks_dir"):
            playbooks_root = bridge._resolve_playbooks_dir()
        else:
            package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            playbooks_root = os.path.join(package_root, "skills", "playbooks")

    contracts = _load_skill_contracts(playbooks_root)
    issues: List[PlanJudgeIssue] = []
    grounded_steps = 0
    step_sequences: Dict[str, List[str]] = {}
    available_agents = set(await bridge.list_agents())
    tools_by_agent: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def get_tool_specs(agent_name: str) -> Dict[str, Dict[str, Any]]:
        if agent_name not in tools_by_agent:
            normalized = _normalize_mcp_tools(await bridge.list_tools(agent_name))
            tool_map = {
                str(tool.get("name")): tool for tool in normalized if str(tool.get("name", "")).strip()
            }
            try:
                resources = await bridge.list_resources(agent_name)
            except Exception:
                resources = []
            if isinstance(resources, list):
                for resource in resources:
                    res_name = getattr(resource, "name", None)
                    if not isinstance(res_name, str) or not res_name.strip():
                        continue
                    synthetic_name = f"{agent_name}__read_resource__{res_name.strip()}"
                    tool_map[synthetic_name] = {
                        "name": synthetic_name,
                        "description": f"Synthetic OpenAI resource-read wrapper for agent resource '{res_name}'.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
            tools_by_agent[agent_name] = tool_map
        return tools_by_agent[agent_name]

    for step in plan.steps:
        contract = contracts.get(step.skill)
        normalized_tool_name, full_tool_name = _normalize_step_tool_name(step.agent, step.tool_name)
        step_is_grounded = False

        if contract is None:
            issues.append(PlanJudgeIssue(
                severity="blocking",
                category="skill",
                step_id=step.step_id,
                skill=step.skill,
                message=f"Skill '{step.skill}' does not exist in the registered playbooks.",
            ))
        else:
            if full_tool_name not in contract.required_tools:
                issues.append(PlanJudgeIssue(
                    severity="blocking",
                    category="grounding",
                    step_id=step.step_id,
                    skill=step.skill,
                    message=f"Step uses tool '{full_tool_name}' which is not declared in skill '{step.skill}'.",
                ))
            else:
                step_is_grounded = True
                grounded_steps += 1
                step_sequences.setdefault(step.skill, []).append(full_tool_name)

        if step.agent not in available_agents:
            issues.append(PlanJudgeIssue(
                severity="blocking",
                category="agent",
                step_id=step.step_id,
                skill=step.skill,
                message=f"Step references unavailable agent '{step.agent}'.",
            ))
            continue

        tool_specs = await get_tool_specs(step.agent)
        tool_spec = tool_specs.get(normalized_tool_name)
        if tool_spec is None:
            issues.append(PlanJudgeIssue(
                severity="blocking",
                category="tool",
                step_id=step.step_id,
                skill=step.skill,
                message=f"Step references unavailable tool '{full_tool_name}'.",
            ))
            continue

    for skill_id, sequence in step_sequences.items():
        contract = contracts.get(skill_id)
        if contract and contract.ordered_tools and not _is_subsequence(sequence, contract.ordered_tools):
            issues.append(PlanJudgeIssue(
                severity="blocking",
                category="order",
                skill=skill_id,
                message=f"Plan steps for skill '{skill_id}' do not follow the tool order declared in the skill contract.",
            ))

    if grounded_steps == len(plan.steps) and grounded_steps > 0:
        grounding_status = "fully_grounded"
    elif grounded_steps > 0:
        grounding_status = "partially_grounded"
    else:
        grounding_status = "ungrounded"

    blocking_issues = [issue for issue in issues if issue.severity == "blocking"]
    if grounding_status == "fully_grounded" and not blocking_issues:
        status = "pass"
        summary = "Plan is fully grounded in registered skills and passed deterministic MCP tool/schema validation."
    elif grounding_status == "partially_grounded":
        status = "revise"
        summary = "Plan is only partially grounded in registered skills and needs revision before approval."
    else:
        status = "block"
        summary = "Plan is not sufficiently grounded in registered skills or failed deterministic tool/schema validation."

    return PlanJudgeReport(
        grounding_status=grounding_status,
        status=status,
        summary=summary,
        issues=issues,
    )
