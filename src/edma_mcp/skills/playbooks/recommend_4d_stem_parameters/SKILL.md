---
id: recommend_4d_stem_parameters
name: Recommend 4D-STEM Experiment Parameters
description: Workflow for checking current UI parameters, collecting required experiment constraints, calculating 4D-STEM/ptychography setup recommendations, validating constraints, and updating the Nion Swift recommendation UI.
version: 1.1.0
---

## Agents Involved
- agent_suggestion

## When To Use
Use this skill when the user asks for 4D-STEM, ptychography, dose, scan-step, convergence-angle, dwell-time, overlap, probe-window, or defocus recommendations before acquisition.

Do not use this skill to acquire data. This skill only recommends and writes experiment setup parameters into the UI.

## Required Tools
- `agent_suggestion.agent_suggestion__read_resource__parameter_info` with arguments `{}` to inspect current essential and recommended parameter values from the UI.
- `agent_suggestion.run_python_code` with arguments `{"code": <python_code_string>}` for free-form numeric calculations, optimization, sensitivity checks, and final constraint validation. The LLM decides the code based on the user's request, UI values, formulas, and constraints in this skill.
- `agent_suggestion.update_parameters` with arguments `{"param_names": [...], "param_values": [...]}` to write recommended values back to the UI.

If the OpenAI wrapper exposes the resource tool under a different name, use the resource URI `nionswift://agent_suggestion/parameter_info` through the available resource-reading tool for `agent_suggestion`.

## Mandatory Inputs
Before recommending or calculating any parameters, confirm that the user or UI has provided all three values:

- `accelerating_voltage_keV`: accelerating voltage in keV.
- `sample_thickness_nm`: sample thickness in nm.
- `dose_max_e_per_A2`: maximum tolerable dose in electrons per square Angstrom.

If any mandatory input is missing, ask only for the missing values. Keep the question short and clear. In a PlanReview, put missing mandatory values in the current step's `required_inputs`; do not block the whole plan with top-level `missing_inputs`.

## Parameters To Recommend
Once the mandatory inputs are complete, recommend every relevant parameter that was not already provided by the user or UI:

- `th_mrad`: convergence semi-angle, range 0 to 40 mrad.
- `camera_dwell_time_s`: camera dwell time in seconds, minimum `2e-4`.
- `ratio`: bright-field disk radius divided by whole detector radius, range 0.5 to 1.
- `fov_A`: probe window / field of view in Angstrom.
- `current_Amps`: probe current in amperes.
- `overlap_coeff`: probe overlap coefficient, range 0 to 1.
- `scan_step_A`: scan step in Angstrom.
- `this_defocus_A`: positive defocus before the sample in Angstrom.
- `total_defocus_A`: downstream-positive total defocus including sample thickness.

## Constants
Use these constants unless the user or UI provides a better value:

```python
detector_max_px = 256
epx_min = 1.0
electron_charge_C = 1.602e-19
```

Compute electron wavelength in Angstrom from accelerating voltage:

```python
wavelength_A = 12.4 / ((2 * 511.0 + acc_voltage_keV) * acc_voltage_keV) ** 0.5
```

## Equations
Use `agent_suggestion.run_python_code` to calculate and optimize when numeric calculation is needed. Do not do numeric optimization only in natural language.

```python
conv_semi_px = detector_max_px * ratio
k_rez_mrad = th_mrad / conv_semi_px
k_rez_A = k_rez_mrad / 1e3 / wavelength_A
fov_A = 1 / k_rez_A
px_size_A = fov_A / (2 * conv_semi_px / ratio)

pattern_dose_e = (current_Amps * camera_dwell_time_s) / electron_charge_C
dose_actual = (current_Amps * camera_dwell_time_s) / (electron_charge_C * scan_step_A**2)
```

Calculate electrons per diffraction pixel:

```python
x = np.arange(-conv_semi_px / ratio, conv_semi_px / ratio)
x, y = np.meshgrid(x, x)
r = (x**2 + y**2) ** 0.5
test_probe_fourier = r < conv_semi_px
pattern_sum = np.sum(test_probe_fourier)
e_per_px = pattern_dose_e / pattern_sum
```

Relate scan step, beam radius, and overlap:

```python
alpha = np.arccos(0.5 * scan_step_A / beam_radius_A)
overlap_coeff = (2 * alpha - np.sin(2 * alpha)) / np.pi
```

Compute defocus and depth-of-field quantities:

```python
this_defocus_A = beam_radius_A / th_mrad * 1e3
total_defocus_A = this_defocus_A + sample_thickness_nm * 10
footprint_beam_after_sample_A = total_defocus_A * th_mrad / 1e3
beam_diameter_A = 2 * beam_radius_A
theta_rad = th_mrad / 1e3
dof_A = 2.0 * wavelength_A / (theta_rad**2)
```

`this_defocus_A` must be positive.

## Hard Constraints
All recommended parameters must satisfy:

- `dose_actual <= dose_max_e_per_A2`.
- `footprint_beam_after_sample_A <= fov_A`.
- `detector_max_px <= 256`.
- `e_per_px >= epx_min`.
- `camera_dwell_time_s >= 2e-4`.
- `0 <= th_mrad <= 40`.
- `0.5 <= ratio <= 1`.
- `0 <= overlap_coeff <= 1`.

## Optimization Targets
Optimize in this priority order:

1. Maximize `e_per_px` while satisfying all hard constraints.
2. Prefer a larger convergence semi-angle when constraints remain satisfied.
3. Keep recommendations physically plausible and consistent with current UI/user values.

## Steps
1. **Read current UI parameters**: Use the parameter-info resource/tool to inspect values already present in the UI.
2. **Calculate and validate recommendations**: Use `agent_suggestion.run_python_code` to merge user/UI context, check mandatory inputs, compute recommended values, optimize within constraints, and validate final metrics. The code should be chosen freely from the user request, UI state, formulas, constraints, and optimization targets in this skill. If mandatory inputs are missing, put only those missing values in this step's `required_inputs` and set `on_failure: ask_user`.
3. **Update UI**: Call `agent_suggestion.update_parameters` with matching `param_names` and `param_values` lists for the recommended values returned by the calculation step.

## PlanReview Guidance
When creating a plan for this skill:

- Use `agent_suggestion` for UI/resource and update steps.
- Include an `agent_suggestion.run_python_code` calculation/validation step before `update_parameters` when numeric recommendation is needed.
- For calculation steps, copy the relevant constants, equations, constraints, optimization targets, current UI values, and explicit user inputs from this skill/request into the handoff. The specialist agent can freely decide the exact calculation/optimization code.
- Do not create abstract non-tool steps such as "merge context", "check inputs", or "explain". These must be folded into the `agent_suggestion.run_python_code` step's goal/expected_output or the final user-facing response.
- Put missing mandatory inputs in `required_inputs` for the `agent_suggestion.run_python_code` step.
- Put UI update failures under `on_failure: ask_user` or `on_failure: stop`, depending on whether the user can correct the issue.
- Do not include acquisition tools in this skill. Acquisition belongs to the 4D-STEM acquisition skill.
- Do not include ptychography reconstruction tools in this skill. Reconstruction belongs to the ptychography skill.
- Do not create a plan that starts at a previous acquisition step number. Recommendation plans must start at step_id `1`.
- If conversation history contains a blocked acquisition step, do not continue that step unless the latest user message explicitly asks to continue acquisition. Parameter suggestion requests should create this recommendation workflow from step_id `1`.

## Success Criteria
- Current UI values were read.
- Mandatory inputs were verified or requested.
- All recommended parameters satisfy hard constraints.
- The final recommendation includes the key constraint metrics: `dose_actual`, `e_per_px`, `footprint_beam_after_sample_A`, `fov_A`, and `dof_A`.
- `agent_suggestion.update_parameters` successfully updates at least one UI parameter.

## Failure Policy
- If mandatory inputs are missing, ask only for the missing values and stop.
- If no feasible parameter set satisfies the hard constraints, explain which constraint is binding and ask the user which constraint can be relaxed.
- If `update_parameters` reports that no UI parameters were found, return that tool output to the user and ask whether to open/initialize the recommendation UI.
- If any MCP tool returns `ok=false` or an error payload, report the failing tool name and exact error message.
