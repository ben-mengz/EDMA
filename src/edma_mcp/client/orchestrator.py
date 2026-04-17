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
    
    instructions = f"""You are the Automated Workflow Architect (Planner). Your sole goal is to discover the right technical skills and design a comprehensive execution blueprint.

### ⚠️ OPERATIONAL PROTOCOL
1. **DISCOVERY FIRST**: You MUST call `list_skills` and `read_skill_content` to understand technical function names. Technical functions are hidden from your default context.
2. **PLAN FORMATION**: Create a multi-step `Plan` (list of `PlanStep` objects).
3. **HAND OFF TO TRIAGE**: Once your plan is ready, you MUST hand off back to the **'Triage'** Agent (e.g. using `transfer_to_MainTriage` or equivalent) with the full `Plan` object as the input. 
4. **NO EXECUTION**: You are FORBIDDEN from attempting to execute tasks or talk to hardware. Your output is a blueprint, not an action.

### 1. AVAILABLE AGENTS (SPECIALIST ROLES)
{registry_summary}
*Note: Technical function names are hidden. You MUST read the Playbooks to see how to invoke specialists.*

### 2. DISCOVERY TOOLS
- `list_skills`: Browse high-level Playbooks.
- `read_skill_content`: Read technical documentation (MANDATORY before planning).

### 3. THE BLUEPRINT LOOP
- **RESEARCH**: Find technical details in SKILL.md.
- **DRAFT**: Construct the logic flow (agent roles, function inputs, on_success/on_failure).
- **DELIVER**: Hand off the plan to Triage for user review.

Remember: You are the ARCHITECT. You design. Triage reviews. Executive executes."""
    return instructions
