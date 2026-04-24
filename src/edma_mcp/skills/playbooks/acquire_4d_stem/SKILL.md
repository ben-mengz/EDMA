---
id: acquire_4d_stem
name: Acquire 4D-STEM on SEM
description: Workflow for starting BF preview, waiting for preview stabilization, synchronizing pixel size, confirming ROI, stopping preview, waiting, and acquiring 4D-STEM data.
version: 1.3.0
---

## Agents Involved
- agent_sem

## Required Tools
- `agent_sem.play_bf` with arguments `{}` to start BF preview.
- `agent_sem.sync_pixel_size` with arguments `{}` to synchronize pixel size from BF preview and recalculate ROI position/size fields.
- `agent_sem.wait` with arguments `{"seconds": <integer>}` for user confirmation gates or required delays.
- `agent_sem.stop_playing_detector` with arguments `{}` to stop BF preview before acquisition.
- `agent_sem.acquire_4d_stem` with arguments `{"defocus_A": <number|null>, "dwell_time": <number|null>, "scan_points_x": <integer|null>, "scan_points_y": <integer|null>, "scan_step_x_A": <number|null>, "scan_step_y_A": <number|null>}`.

## Steps
1. **Start BF preview**: Call `agent_sem.play_bf`.
2. **Wait for BF preview stabilization**: Immediately after `agent_sem.play_bf` succeeds, call `agent_sem.wait` with arguments `{"seconds": 10}`. This 10 second wait is mandatory before pixel-size synchronization.
3. **Synchronize pixel size**: Call `agent_sem.sync_pixel_size`. This must target the BF preview data item, update BF preview calibration, and recalculate ROI position/size fields in the UI.
4. **Ask user to confirm ROI selection**: After `agent_sem.sync_pixel_size` succeeds, pause and ask the user to confirm that the ROI shown on BF preview is the intended scan region. Represent this as a PlanStep using `agent_sem.wait` with arguments `{"seconds": 1}` and put `confirm_roi_selection_after_pixel_sync` in `required_inputs`. Do not proceed until the user confirms.
5. **Stop BF preview**: Call `agent_sem.stop_playing_detector`.
6. **Wait after stopping preview**: Immediately after `agent_sem.stop_playing_detector` succeeds, call `agent_sem.wait` with arguments `{"seconds": 10}`. This 10 second wait is mandatory.
7. **Acquire Data**: Call `agent_sem.acquire_4d_stem`. For scan grid, one X-direction value (`scan_points_x` or `scan_step_x_A`) and one Y-direction value (`scan_points_y` or `scan_step_y_A`) are sufficient. If user did not provide scan grid values, call with JSON `null` or omit those keys so the tool checks existing UI fields. If neither chat input nor UI contains one value per direction, the tool will ask for the missing X/Y scan grid input. The UI auto-calculates the paired value from ROI size when one value per direction is updated.

## Planning Contract
- The planner must not add steps outside the seven steps above.
- For event-triggered continuation, if UI action context shows that one or more earlier steps already completed successfully, omit those completed steps and plan only the remaining suffix of the seven-step workflow. Keep the original logical step order, but the PlanReview may start at the next incomplete step.
- The planner must use the exact tool names above. Do not use old names such as `sem_agent`, `turn_on_inlens_detector`, `confirm_roi_selection`, `apply_roi_parameters`, `calculate_scan_parameters`, or `acquire_4d_stem_data`.
- Do not call `agent_sem.get_sem_state` in this workflow unless the user explicitly asks to inspect SEM state as a separate request.
- Do not call `agent_sem.calculate_scan_parameters` in this workflow. The UI already performs scan-grid auto-calculation when scan points or scan step fields are updated.
- The only ROI confirmation is the chat/user-confirmation gate immediately after `agent_sem.sync_pixel_size`; do not use or invent a ROI confirmation tool.
- The wait after starting BF preview must be exactly `agent_sem.wait` with arguments `{"seconds": 10}` before `agent_sem.sync_pixel_size`.
- The wait after stopping BF preview must be exactly `agent_sem.wait` with arguments `{"seconds": 10}`.
- Optional numeric arguments must be JSON numbers when known and JSON `null` or omitted when unknown. Do not use the string `"None"`.
- Do not add skip-acquisition, existing-data checks, SEM state checks, final validation, reconstruction, or parameter recommendation steps unless the user explicitly selects another skill that contains those steps.
- `list_agents_capabilities` may be used only to verify that the required tools exist; it must not be used to add extra tools to this workflow.

## Completed Action Mapping
Use these UI/action events as evidence of completed workflow steps:

- `agent_sem.trigger_llm play_bf`, `tool_name=play_bf`, or "BF Preview playback started" means step 1 is complete; the next planned step should be step 2 (`agent_sem.wait {"seconds": 10}`).
- `agent_sem.trigger_llm sync_pixel_size`, `tool_name=sync_pixel_size`, or "Pixel size sync" means step 3 is complete; the next planned step should be step 4, asking the user to confirm ROI selection.
- `agent_sem.trigger_llm stop_bf`, `tool_name=stop_bf`, `tool_name=stop_playing_detector`, or "BF Preview playback stopped" means step 5 is complete; the next planned step should be step 6 (`agent_sem.wait {"seconds": 10}`).
- `agent_sem.trigger_llm acquire_4d_stem` with an acquisition-started or acquisition-finished message means step 7 has started or completed; do not plan a duplicate acquisition unless the user explicitly asks for another run.

## Success Criteria
- BF preview starts.
- 10 seconds are waited after starting BF preview and before pixel-size synchronization.
- Pixel size is synchronized from BF preview and ROI position/size fields are recalculated.
- User confirms the BF preview ROI after pixel-size synchronization.
- BF preview is stopped.
- 10 seconds are waited after stopping BF preview.
- 4D-STEM acquisition returns a saved data path or success message.

## Failure Policy
- If pixel size synchronization, ROI confirmation, defocus, dwell, or scan grid inputs are missing, put them in the relevant step's `required_inputs` list and use `on_failure: ask_user`; do not block the whole plan with PlanReview `missing_inputs`.
- If any microscope tool fails, stop the workflow unless the user provides recovery instructions.
