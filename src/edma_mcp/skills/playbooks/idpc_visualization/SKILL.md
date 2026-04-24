---
id: idpc_visualization
name: iDPC Visualization
description: Calibrate 4D-STEM iDPC inputs and generate an iDPC-style image from the selected dataset.
version: 1.1.0
---

## Agents Involved
- agent_visulization

## When To Use
Use this skill when the user wants to generate an iDPC-style image from the currently selected 4D-STEM dataset.

Do not use this skill for BF, ABF, or ADF virtual detector images.
Do not use this skill for ptychography result review.

## Required Tools
- `agent_visulization.idpc_calibration` with arguments `{"convergence_angle": <number|null>, "mrad_per_pixel": <number|null>, "scan_step_A": <number|null>, "fov_nm": <number|null>}`.
- `agent_visulization.idpc_generate` with arguments `{"rotation_scan": <number|string|null>, "convergence_angle": <number|null>, "mrad_per_pixel": <number|null>, "scan_step_A": <number|null>, "fov_nm": <number|null>}`.

## Steps
1. **Calibrate iDPC inputs**: Call `agent_visulization.idpc_calibration`. Use user-provided values when present. Otherwise use JSON `null` so the tool can infer values from the selected dataset and metadata.
2. **Generate iDPC image**: Call `agent_visulization.idpc_generate` using the calibrated values and the requested rotation.

## Planning Contract
- The planner must use the exact tool names above. Do not use old names such as `visualize_idpc` or `visualize_4dstem`.
- Do not skip the calibration step. The planner must call `agent_visulization.idpc_calibration` before `agent_visulization.idpc_generate`.
- `rotation_scan` may be a numeric value or the string `"auto"`.
- The planner must not add extra SEM state checks, resource reads, waits, or validation steps unless the user explicitly asks for troubleshooting.
- Optional numeric arguments must be JSON numbers when known and JSON `null` or omitted when unknown. Do not use the string `"None"`.
- If scan calibration or diffraction calibration is missing, let the calibration tool report the missing input; do not invent defaults.
- If the user does not specify a rotation, use `rotation_scan="auto"`.

## Success Criteria
- Scan inputs are calibrated or validated.
- Diffraction calibration is resolved from dataset calibration or user input.
- The iDPC image is displayed.

## Failure Policy
- If the selected data is not valid 4D-STEM data, stop and ask the user to select the correct dataset.
- If convergence angle, mrad/pixel, scan step, FOV, or rotation is required and missing, ask the user for the exact missing value.
