---
id: ptychography_on_sem
name: Ptychography on SEM
description: A compound skill that first acquires data on the SEM and then processes it using the ptychography workflow.
version: 1.0.0
---

## Agents Involved
- sem_agent
- ptychography_agent

## Steps
1. **Acquire SEM Data**: Run the `acquire_4d_stem` skill to get the raw data from the microscope.
2. **Process Ptychography**: Run the `ptychography` skill to manage parameters and send the data for reconstruction.
