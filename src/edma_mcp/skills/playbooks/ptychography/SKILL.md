---
id: ptychography
name: Ptychography Parameter Preparation And Submission
description: Prepare ptychography reconstruction parameters, append experimental parameters, save them, and submit reconstruction to the server.
version: 1.1.0
---

## Agents Involved
- agent_ptychography

## Required Tools
- `agent_ptychography.prepare_parameters` with arguments `{"total_thickness": <number|null>, "num_slices": <integer|null>, "convergence_angle": <number|null>, "accela_voltage": <number|null>, "fov_nm": <number|null>, "PLRotation_deg": <number|string|null>, "rez_pixel_size_mrad": <number|null>, "rez_pixel_size_A": <number|null>, "aberrations": <array>, "data_pad": <integer|null>, "bright_threshold": <number>, "scan_step_A": <number|null>, "transform_axis_matrix": <array>}`.
- `agent_ptychography.append_exp_params` with arguments `{}`.
- `agent_ptychography.save_params` with arguments `{}`.
- `agent_ptychography.start_server_ptychography` with arguments `{"param_path": "<path returned by save_params or current ptychography UI path_params>"}`.
- Optional support tool: `agent_ptychography.load_preset` with arguments `{}` when the user wants to start from a preset.
- Optional support tool: `agent_ptychography.search_RAG_for_parameters` with arguments `{"parameter_name_list": ["<parameter names>"]}` when parameter meanings/defaults are unclear.
- Optional support tool: `agent_ptychography.change_pypty_parameters` with arguments `{"user_query": "<requested parameter change>"}` when the user asks to modify current pypty or experimental parameters.

## Steps
1. **Prepare essential and experimental parameters**: Call `agent_ptychography.prepare_parameters`. Use values from user input, current ptychography UI state, SEM/acquisition metadata, or JSON `null` for unknown optional numeric values. Use current UI/default values for non-null array/number fields such as `aberrations`, `bright_threshold`, and `transform_axis_matrix`.
2. **Append experimental parameters after review**: Call `agent_ptychography.append_exp_params`, but put `confirm_prepared_ptychography_parameters` in this step's `required_inputs` so execution stops and asks the user to review the prepared parameters before calling the tool.
3. **Save parameters**: Call `agent_ptychography.save_params`. The output should include or imply the parameter file path.
4. **Submit reconstruction after saved-parameter review**: Call `agent_ptychography.start_server_ptychography` with `param_path` from `save_params` output or the current ptychography UI `experimental_params.path_params`, but put `confirm_saved_ptychography_parameters_before_server` in this step's `required_inputs` so execution stops and asks the user before server submission.

## Planning Contract
- The planner must use the exact tool names above. Do not use old names such as `check_parameters`, `append_parameter`, `save_parameters`, or `send_to_server`.
- Do not collapse `prepare_parameters` and `append_exp_params`; they are separate MCP tools and separate workflow steps.
- Do not add separate no-op confirmation steps. Use `required_inputs` on `append_exp_params` for confirming prepared parameters, and on `start_server_ptychography` for confirming saved parameters.
- Optional numeric arguments must be JSON numbers when known and JSON `null` or omitted when unknown. Non-null fields (`aberrations`, `bright_threshold`, `transform_axis_matrix`) must be populated from UI/defaults. Do not use the string `"None"`.
- If the user asks to explain or modify a ptychography parameter, use `search_RAG_for_parameters` or `change_pypty_parameters` as a support step only when needed.

## Success Criteria
- Essential and experimental ptychography parameters are prepared for the selected 4D-STEM dataset.
- Experimental parameters are appended into the pypty parameter dictionary.
- Parameters are saved to disk.
- Reconstruction job is submitted to the server.

## Failure Policy
- If required reconstruction parameters are missing, put them in the relevant step's `required_inputs` list and use `on_failure: ask_user`; do not block the whole plan with PlanReview `missing_inputs`.
- If parameter preparation, appending, saving, or server submission fails, stop the workflow and report the exact failing tool/error.
