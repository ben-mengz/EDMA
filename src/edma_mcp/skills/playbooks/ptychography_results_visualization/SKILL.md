---
id: ptychography_results_visualization
name: Ptychography Results Visualization
description: Query recent ptychography slurm IDs and visualize reconstruction results such as object, probe, positions, or loss curve.
version: 1.1.0
---

## Agents Involved
- agent_visulization

## When To Use
Use this skill when the user wants to inspect ptychography reconstruction results that already exist on the server.

Use this skill for object, probe, positions, or loss-curve visualization.

Do not use this skill to submit new ptychography jobs.
Do not use this skill for 4D-STEM virtual detector or iDPC generation.

## Required Tools
- `agent_visulization.list_ptychography_result_ids` with arguments `{"limit": <integer>, "offset": <integer>}`.
- `agent_visulization.visualize_ptychography_results` with arguments `{"slurm_id": "<string>", "to_visualization": "<object|probe|positions|Loss Curve>", "epoch": "<final|integer>", "start_slices_obj": <integer>, "end_slices_obj": <integer>}`.

## Steps
1. **Query available slurm IDs**: Call `agent_visulization.list_ptychography_result_ids` to inspect recent reconstruction jobs.
2. **Visualize the requested result**: Call `agent_visulization.visualize_ptychography_results` with the selected `slurm_id` and requested visualization mode.

## Planning Contract
- The planner must use the exact tool names above. Do not use extra remote-inspection tools outside this skill.
- Do not call `agent_visulization.visualize_ptychography_results` before a slurm ID is known.
- Use `epoch="final"` unless the user explicitly asks for a checkpoint epoch.
- Use `to_visualization="object"`, `"probe"`, `"positions"`, or `"Loss Curve"` exactly.
- Keep `start_slices_obj` and `end_slices_obj` at their default values unless the user explicitly requests an object-slice subset.
- The planner must not add extra server checks, wait steps, or filesystem validation steps beyond the two skill steps above unless the user explicitly asks for troubleshooting.
- Optional numeric arguments must be JSON numbers when known and JSON `null` or omitted when unknown. Do not use the string `"None"`.
- If the user does not provide a slurm ID, ask the user to choose one from the queried results before the visualization step.

## Success Criteria
- Recent ptychography slurm IDs can be queried.
- The requested reconstruction result or loss curve is loaded and displayed.

## Failure Policy
- If the server is unreachable, stop and report that the results server cannot be reached.
- If the slurm ID is missing or invalid, ask the user for a valid slurm ID.
- If the requested result file is missing, report the missing file and stop.
