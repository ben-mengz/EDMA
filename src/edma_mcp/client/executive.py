import json
from typing import Dict, Any, List, Optional
from agents import Agent
from agents.handoffs import handoff

class ExecutiveAgentLogic:
    """Logic for the Executive (Executor) Agent to process JSON plans."""

    @staticmethod
    def get_instructions() -> str:
        return """You are the Executive Agent. Your role is to EXECUTE a structured plan provided by the Orchestrator.
        
YOU WILL RECEIVE:
A JSON-like structure (ExecutionPlan) containing a 'goal' and a list of 'steps'.

YOUR PROCESS:
1. Parse the provided plan. IMPORTANT: Since this is a structured handoff, you must look at the arguments (goal, steps) provided in the 'transfer_to_executive' tool call from the previous turn.
2. Track the current execution stage globally.
3. For the CURRENT step, use the `handoff` tool to transfer control to the specialist agent mentioned in that step.
4. Pass the specific instructions for that step to the specialist: "Execute this: [actions/query]. Once finished, handoff back to the Executive."
5. When the specialist returns control, determine if the step succeeded.
6. Look at 'on_success' or 'on_failure' to determine the NEXT step number (or 'done'/'stop').
7. Once the sequence reaches 'done' or 'stop', provide a FINAL SUMMARY to the user and handoff back to the Triage Agent.

Strict rule: You are an EXECUTOR. Do not deviate from the steps unless the specialist fails and the plan says to stop or ask the user."""

    @staticmethod
    def parse_plan(message: str) -> Optional[Dict[str, Any]]:
        """
        Extract JSON plan from the orchestrator's message.
        With structured handoff, the message itself should be or contain the JSON object.
        """
        try:
            # First try standard JSON load on the whole string
            return json.loads(message)
        except:
            pass

        try:
            # Fallback: Look for the first JSON-like block
            start = message.find('{')
            end = message.rfind('}') + 1
            if start != -1 and end != -1:
                return json.loads(message[start:end])
        except:
            pass
        return None

# The Executive Agent will be instantiated in openai_bridge.py
