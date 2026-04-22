---
id: ptychography_on_sem
name: Ptychography on SEM
description: A compound skill that first acquires data on the SEM and then processes it using the ptychography workflow.
version: 1.0.0
---

## Agents Involved
- agent_sem
- agent_ptychography

## Required Tools
- This is a compound skill. The planner must call `read_skill_content` for `acquire_4d_stem` and `ptychography` before drafting the plan.
- For SEM acquisition, use exactly the tools and argument shapes listed in the `acquire_4d_stem` skill.
- For ptychography reconstruction, use exactly the tools and argument shapes listed in the `ptychography` skill.
- Do not add tools that are not listed by those two referenced skills.

## Steps
1. **Acquire SEM Data**: Expand the full `acquire_4d_stem` skill steps into this plan, preserving its tool order and required user-confirmation gates.
2. **Process Ptychography**: Expand the full `ptychography` skill steps after acquisition, using the acquired 4D-STEM data path when a ptychography step requires experimental data.

## Planning Contract
- The planner must expand referenced skills into concrete PlanSteps. Do not leave a high-level "run acquire_4d_stem" or "run ptychography" placeholder step.
- Do not insert SEM state checks, existing-data checks, skip-acquisition prompts, scan-parameter calculation steps, ROI confirmation tools, final validation steps, or extra waits unless one of the referenced skills explicitly lists them.
- Do not use `agent_sem.get_sem_state` or `agent_sem.calculate_scan_parameters` for this compound workflow unless the user explicitly asks for those as a separate requirement.
- If the user explicitly says they already have selected/ready 4D-STEM data and wants to skip acquisition, omit the `acquire_4d_stem` skill and plan only the `ptychography` skill. Do not add an SEM state check to decide this.
- The output of SEM acquisition may be referenced by later ptychography steps as a placeholder such as `{{acquire_4d_stem.data_path}}` when the exact data path is not known at planning time.

## Success Criteria
- 4D-STEM data is acquired.
- Ptychography parameters are validated and saved.
- Reconstruction job is submitted.

## Failure Policy
- If SEM acquisition fails, stop before reconstruction.
- If reconstruction parameters are missing, ask the user before submitting.
