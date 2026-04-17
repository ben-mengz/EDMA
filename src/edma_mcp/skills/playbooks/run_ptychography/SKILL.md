---
id: run_ptychography
name: Run Ptychography Workflow
description: Execute a full ptychography workflow including parameter setup and reconstruction.
version: 1.0.0
---

## Agents Involved
- ptychography_agent

## Steps
1. **Initialize Parameters**: Set up the initial reconstruction conditions.
2. **Execute Reconstruction**: Call the `save_parameters` and `send_to_server` tools via `ptychography_agent`.
