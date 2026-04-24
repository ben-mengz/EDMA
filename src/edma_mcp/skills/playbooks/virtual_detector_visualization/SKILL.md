---
id: virtual_detector_visualization
name: Virtual Detector Visualization
description: Calibrate 4D-STEM virtual detector inputs and generate a BF, ABF, or ADF virtual detector image from the selected dataset.
version: 1.1.0
---

## Agents Involved
- agent_visulization

## When To Use
Use this skill when the user wants to display a virtual detector image from the currently selected 4D-STEM dataset.

Use this skill for BF, ABF, or ADF-style virtual detector images.

Do not use this skill for iDPC generation.
Do not use this skill for ptychography result review.

## Required Tools
- `agent_visulization.virtual_detector_calibration` with arguments `{"convergence_angle": <number|null>, "mrad_per_pixel": <number|null>, "scan_step_A": <number|null>, "fov_nm": <number|null>}`.
- `agent_visulization.virtual_detector_generate` with arguments `{"inner_angle": <number|null>, "outer_angle": <number|null>, "region_mode": "<inner|middle|outer>", "convergence_angle": <number|null>, "mrad_per_pixel": <number|null>, "scan_step_A": <number|null>, "fov_nm": <number|null>}`.

## Steps
1. **Calibrate virtual detector inputs**: Call `agent_visulization.virtual_detector_calibration`. Use user-provided values when present. Otherwise use JSON `null` so the tool can infer values from the selected dataset and metadata.
2. **Generate virtual detector image**: Call `agent_visulization.virtual_detector_generate` using the calibrated values and the requested detector region.

## Planning Contract
- The planner must use the exact tool names above. Do not use old names such as `generate_virtual_image` or `visualize_4dstem`.
- Do not skip the calibration step. The planner must call `agent_visulization.virtual_detector_calibration` before `agent_visulization.virtual_detector_generate`.
- Use `region_mode="inner"` for BF.
- Use `region_mode="middle"` for ABF.
- Use `region_mode="outer"` for ADF.
- The planner must not add extra SEM state checks, resource reads, waits, or validation steps unless the user explicitly asks for troubleshooting.
- Optional numeric arguments must be JSON numbers when known and JSON `null` or omitted when unknown. Do not use the string `"None"`.
- If scan calibration or diffraction calibration is missing, let the calibration tool report the missing input; do not invent defaults.
- If the user does not specify BF, ABF, or ADF, ask which detector region they want before the generate step.

## Success Criteria
- Scan inputs are calibrated or validated.
- Diffraction calibration is resolved from dataset calibration or user input.
- The requested BF, ABF, or ADF virtual detector image is displayed.

## Failure Policy
- If the selected data is not valid 4D-STEM data, stop and ask the user to select the correct dataset.
- If convergence angle, mrad/pixel, scan step, FOV, inner angle, or outer angle is required and missing, ask the user for the exact missing value.
