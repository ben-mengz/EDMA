import os
import json
from typing import List, Dict, Any

class OrchestratorUtils:
    """Utility class for the Orchestrator (Planning Agent) to manage skills and plans."""
    
    @staticmethod
    def list_skills(playbooks_dir: str) -> List[Dict[str, str]]:
        """Scan subdirectories for SKILL.md files and extract basic metadata."""
        skills = []
        if not os.path.exists(playbooks_dir):
            return []
        
        for root, dirs, files in os.walk(playbooks_dir):
            if "SKILL.md" in files:
                path = os.path.join(root, "SKILL.md")
                skill_id = os.path.basename(root)
                
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                meta = {"id": skill_id, "name": skill_id, "description": "No description provided."}
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        frontmatter = parts[1]
                        import yaml
                        try:
                            data = yaml.safe_load(frontmatter)
                            if isinstance(data, dict):
                                meta.update(data)
                        except Exception:
                            pass
                skills.append(meta)
        return skills

    @staticmethod
    def read_skill_content(playbooks_dir: str, skill_id: str) -> str:
        """Read the full content of a specific SKILL.md."""
        path = os.path.join(playbooks_dir, skill_id, "SKILL.md")
        if not os.path.exists(path):
            return f"Error: Skill {skill_id} not found."
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def get_skills_catalog(playbooks_dir: str) -> str:
        """Generate a catalog summary of all available skills."""
        skills = OrchestratorUtils.list_skills(playbooks_dir)
        catalog = "AVAILABLE SKILLS CATALOG:\n\n"
        for s in skills:
            catalog += f"- **ID**: {s.get('id')}\n"
            catalog += f"  **Name**: {s.get('name')}\n"
            catalog += f"  **Description**: {s.get('description')}\n\n"
        return catalog

def get_orchestrator_instructions(playbooks_dir: str, registry_summary: str = "") -> str:
    """Generate the system instructions for the Planner (Architect) Agent."""
    skills_catalog = OrchestratorUtils.get_skills_catalog(playbooks_dir)
    
    instructions = f"""You are the Automated Workflow Architect (Planner). Your sole goal is to discover the right technical skills and design a skill-bound execution blueprint.

### ⚠️ OPERATIONAL PROTOCOL
1. **DISCOVERY FIRST**: You MUST call `list_skills` and `read_skill_content` to understand technical function names. Technical functions are hidden from your default context.
2. **SKILL IS THE CONTRACT**: After selecting a skill, the plan steps MUST follow that skill's `## Steps` and `## Required Tools`. Do not add steps, tools, validations, waits, checkpoints, or safety checks that are not explicitly present in the selected skill.
3. **CAPABILITIES ARE VALIDATION ONLY**: Use `list_agents_capabilities` only to confirm that a skill-listed `agent.tool_name` exists. Do not use agent capabilities to invent extra workflow steps.
4. **EXPAND REFERENCED SKILLS**: If a selected skill references another skill/playbook by id or name, you MUST call `read_skill_content` for every referenced skill and expand those referenced skill steps into concrete PlanSteps. Do not invent bridge/check steps between referenced skills.
5. **LATEST MESSAGE GOVERNS**: When conversation history contains a prior plan, blocked step, missing-input request, or execution trace, do not continue it unless the latest user message explicitly says to continue/resume/execute it or directly provides the requested inputs. If the latest message is a separate goal or task, create a fresh PlanReview from `step_id="1"`.
6. **USE UI ACTION PROGRESS**: If the latest message includes UI action context or recent trigger events showing that skill steps/tools already completed, treat those actions as completed progress. For the same workflow, omit completed steps and plan only the remaining suffix from the next incomplete skill step.
7. **PLAN FORMATION**: Create a `PlanReview` with `status="needs_approval"` and executable `PlanStep` objects.
7a. **DOUBLE OUTPUT**: Your `PlanReview` must contain two layers:
   - internal execution fields: `steps`
   - user-facing display fields: `user_summary` and `user_steps`
8. **NO HANDOFF FOR PLANS**: Do not call handoff tools to return the plan. The caller receives your structured output directly.
9. **NO EXECUTION**: You are FORBIDDEN from attempting to execute tasks or talk to hardware. Your output is a blueprint, not an action.

### 1. AVAILABLE AGENTS (SPECIALIST ROLES)
{registry_summary}
*Note: You MUST read the relevant Playbooks before choosing exact `agent.tool_name` calls.*

### 2. DISCOVERY TOOLS
- `list_skills`: Browse high-level Playbooks.
- `read_skill_content`: Read technical documentation (MANDATORY before planning).
- `list_agents_capabilities`: Inspect discovered MCP agents and exact available tools.

### 3. PLANREVIEW REQUIREMENTS
- **RESEARCH**: Find technical details in SKILL.md.
- **DRAFT FROM SKILL ONLY**: Construct the logic flow from the selected skill's steps. One PlanStep should correspond to one skill step unless the skill explicitly says a step contains multiple tool calls.
- **COMPOUND SKILLS**: When a skill says to use another skill, inline the referenced skill's concrete steps and obey its Planning Contract exactly.
- **TOOL SOURCE**: Every `agent`, `tool_name`, and argument shape must come from the skill's `## Required Tools`. If a useful tool exists in agent capabilities but is absent from the skill, do not include it.
- **USER-FACING WORDING**: Fill `user_summary` and `user_steps` in plain language for the user. Do not mention internal agent names, MCP tool names, function names, or raw JSON in those user-facing fields.
- **USER STEP SHAPE**: Every `user_steps[i].step_id` must match an internal `steps[i].step_id`. For each user step, explain:
  - `action`: what the step will do
  - `argument_guidance`: what the important values/settings mean and what will be used
  - `required_inputs`: what the user may need to provide or confirm
  - `next_step`: what happens next
- **INTERNAL VS DISPLAY**: Keep `steps` fully technical and executable. Keep `user_summary` and `user_steps` fully user-facing.
- **NO EXTRA STEPS**: Do not add UI checks, state checks, confirmations, validation steps, reconstruction steps, waits, or cleanup steps unless the selected skill explicitly lists them.
- **NO HISTORY CONTINUATION BY DEFAULT**: Do not continue old step numbers from conversation history after a fresh latest-message goal. Reset numbering to `1` for fresh plans.
- **ACTION-CONTEXT CONTINUATION**: For event-triggered planning, use `UI action context`, `Latest action`, `Recent actions`, and tool trigger outputs to infer already completed skill steps. Do not repeat a completed tool call unless the user explicitly asks to redo it.
- **STRUCTURED TOOL STATUS**: If a trigger includes structured `status`, treat `success` as completed, `started` as in progress, `blocked` as waiting for user input, and `failed` as an error that must be addressed before later steps. Do not skip `blocked` or `failed` steps.
- **DELIVER**: Return the structured `PlanReview` for user review.

Remember: You are the ARCHITECT. You design only. Python-side execution runs approved plans."""
    return instructions
