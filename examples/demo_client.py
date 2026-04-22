import os
import asyncio
import json
import tkinter as tk
from tkinter import scrolledtext
from typing import Dict, Any, Optional
import threading

# Import the new OpenAI Agents Python library
try:
    from agents import Agent, Runner
    HAS_AGENTS = True
except ImportError:
    HAS_AGENTS = False

from edma_mcp.client.openai_bridge import OpenAIMCPBridge
from edma_mcp.client.eventHubListener import EventHubListener, EventHubConfig, EventDispatcher
from edma_mcp.client.models import PlanExecutionResult, PlanReview
from edma_mcp.client.plan_executor import PlanExecutor
from edma_mcp.client.thread_helper import ThreadHelper

class EDMAChatApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EDMA Client (OpenAI Agents Brain)")
        self.root.geometry("750x650")

        # Layout
        # 1. API Key Frame
        top_frame = tk.Frame(root, pady=5, padx=10)
        top_frame.pack(fill="x")
        
        tk.Label(top_frame, text="OpenAI API Key: ").pack(side="left")
        self.api_key_var = tk.StringVar(value=os.environ.get("OPENAI_API_KEY", ""))
        self.api_entry = tk.Entry(top_frame, textvariable=self.api_key_var, width=50, show="*")
        self.api_entry.pack(side="left", padx=5)

        # 2. Chat Log
        chat_frame = tk.Frame(root, padx=10, pady=5)
        chat_frame.pack(fill="both", expand=True)
        self.chat_log = scrolledtext.ScrolledText(chat_frame, state="disabled", wrap="word", bg="#1e1e1e", fg="#00ff00", font=("Consolas", 10))
        self.chat_log.pack(fill="both", expand=True)

        # 3. Interactive Input Frame (Like a normal MCP Client Chat)
        input_frame = tk.Frame(root, padx=10, pady=10)
        input_frame.pack(fill="x")
        
        self.user_input = tk.Entry(input_frame, font=("Consolas", 12))
        self.user_input.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.user_input.bind("<Return>", lambda e: self.send_manual_message())
        
        btn_send = tk.Button(input_frame, text="Chat locally", bg="#005500", fg="black", command=self.send_manual_message)
        btn_send.pack(side="right")
        
        # Internals
        # Replace general MCPBridgeManager with our advanced OpenAIMCPBridge!
        self.bridge = OpenAIMCPBridge(base_http_url="http://127.0.0.1:7300")
        
        self.dummy_loop = asyncio.new_event_loop()
        self.thread_helper = ThreadHelper(main_event_loop=self.dummy_loop)
        
        self.dispatcher = EventDispatcher()
        self.setup_dispatcher()
        self.start_listener()
        
        # Periodically pump the asyncio loop so that Main Thread calls from ThreadHelper get executed
        def pump_loop():
            self.dummy_loop.call_soon(self.dummy_loop.stop)
            self.dummy_loop.run_forever()
            self.root.after(5, pump_loop)
        self.root.after(5, pump_loop)
        
        self.log_system("System initialized. Waiting for UI Server to send events, or type a message below...", "system")
        self.history = []
        self.pending_plan: Optional[PlanReview] = None
        if not HAS_AGENTS:
            self.log_system("Warning: 'openai-agents' Python package not found. Please 'pip install openai-agents' to use the SDK.", "error")
        else:
            self.log_system("OpenAI Agents SDK detected. Ready to process tool triggers/chat.", "system")

    def log_system(self, text: str, tag: str = "normal"):
        """Thread-safe UI logging using ThreadHelper natively bridging to Tkinter"""
        def _log():
            self.chat_log.config(state="normal")
            prefixes = {
                "system": "\n⚙️ [System] ",
                "ui": "\n📱 [Remote UI Action] ",
                "user": "\n🧑‍💻 [You] ",
                "llm": "\n🤖 [Agent Response] ",
                "tool": "\n🔧 [Action Taken] ",
                "error": "\n❌ [Error] ",
                "normal": "\n"
            }
            prefix = prefixes.get(tag, "\n")
            self.chat_log.insert(tk.END, prefix + text + "\n")
            self.chat_log.see(tk.END)
            self.chat_log.config(state="disabled")
            
        self.thread_helper.call_on_main_thread(_log)

    def send_manual_message(self):
        """Called when user types in the input box and hits Enter or Chat locally."""
        msg = self.user_input.get().strip()
        if not msg:
            return

        self.user_input.delete(0, tk.END)
        self.log_system(msg, "user")

        if self.pending_plan and self._is_approval_message(msg):
            self.thread_helper.submit_async(self.execute_pending_plan())
            return

        if not HAS_AGENTS:
            self.log_system("Cannot trigger LLM: 'agents' package missing.", "error")
            return
            
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self.log_system("Cannot trigger LLM: API Key is empty!", "error")
            return

        os.environ["OPENAI_API_KEY"] = api_key
        # Reuse the exact same async function we use for Remote triggers
        self.thread_helper.submit_async(self.run_agents_framework(msg))

    def _is_approval_message(self, msg: str) -> bool:
        return msg.strip().lower() in {"批准", "执行", "approve", "approved", "go", "yes", "run"}

    def _extract_plan_review(self, output: Any) -> Optional[PlanReview]:
        if isinstance(output, PlanReview):
            return output
        if hasattr(output, "model_dump"):
            try:
                return PlanReview.model_validate(output.model_dump())
            except Exception:
                pass
        if isinstance(output, str):
            text = output.strip()
            candidates = [text]
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidates.append(text[start:end + 1])
            for candidate in candidates:
                try:
                    return PlanReview.model_validate_json(candidate)
                except Exception:
                    try:
                        return PlanReview.model_validate(json.loads(candidate))
                    except Exception:
                        continue
        return None

    def _render_plan_review(self, plan: PlanReview) -> str:
        lines = [
            f"Plan awaiting approval: {plan.goal}",
            "",
            plan.summary,
            "",
            "Steps:",
        ]
        for step in plan.steps:
            lines.append(
                f"{step.step_id}. {step.agent}.{step.tool_name} "
                f"args={json.dumps(step.arguments, ensure_ascii=False)} -> {step.on_success}"
            )
            lines.append(f"   Goal: {step.goal}")
            lines.append(f"   Expected: {step.expected_output}")
            if step.required_inputs:
                lines.append("   Ask at this step: " + "; ".join(step.required_inputs))
            lines.append(f"   On failure: {step.on_failure}")
        if plan.risks:
            lines.extend(["", "Risks / assumptions:"])
            lines.extend(f"- {item}" for item in plan.risks)
        lines.extend(["", "Reply with 'approve' or 'go' to execute this plan. Step-specific missing inputs will be requested only when that step is reached."])
        return "\n".join(lines)

    def _render_execution_result(self, result: PlanExecutionResult) -> str:
        lines = [
            f"Execution status: {result.status}",
            result.message,
            "",
            "Step results:",
        ]
        for item in result.results:
            lines.append(f"- {item.step_id} {item.agent}.{item.tool_name}: {item.status}")
            lines.append(f"  {item.result}")
        return "\n".join(lines)

    def setup_dispatcher(self):
        @self.dispatcher.on("show")
        def handle_show(event: Dict[str, Any]):
            print(2)
            agent = event.get('agent', 'unknown')
            msg = event.get('payload', {}).get('message', '')
            print(f"[DEBUG UI] Received show event: {event}", flush=True)
            self.log_system(f"Received SHOW from '{agent}':\n > {msg}", "ui")

        @self.dispatcher.on("trigger")
        def handle_trigger(event: Dict[str, Any]):
            print(1)
            if not HAS_AGENTS:
                self.log_system("Cannot trigger LLM: 'agents' package missing.", "error")
                
                return
                
            api_key = self.api_key_var.get().strip()
            if not api_key:
                self.log_system("Cannot trigger LLM: API Key is empty!", "error")
                
                return

            agent = event.get('agent', 'unknown')
            msg = event.get('payload', {}).get('tool_call_message', {}).get('trigger_message', 'No message')
            
            self.log_system(f"Received remote TRIGGER via '{agent}':\n > Instruction: {msg}", "ui")
            
            # Start the OpenAI Swarm agent loop in the background!
            os.environ["OPENAI_API_KEY"] = api_key
            self.thread_helper.submit_async(self.run_agents_framework(msg))

        @self.dispatcher.set_default
        def handle_default(event: Dict[str, Any]):
            self.log_system(f"Unhandled event: {event}", "system")

    async def execute_pending_plan(self):
        if not self.pending_plan:
            self.log_system("No pending plan to execute.", "error")
            return
        plan = self.pending_plan
        self.pending_plan = None
        self.log_system("Executing approved plan via direct MCP tool calls...", "system")
        try:
            executor = PlanExecutor(self.bridge)
            result = await executor.execute(plan)
            self.log_system(self._render_execution_result(result), "llm")
        except Exception as e:
            self.log_system(f"Plan execution failed: {e}", "error")

    async def run_agents_framework(self, user_message: str):
        """
        Uses the `OpenAIMCPBridge` we made earlier, which directly translates all
        Server agents -> OpenAI Agent objects and connects them with handoffs.
        We then run it using `Runner`.
        """
        self.log_system("Building planning-enabled Multi-Agent Network via FastMCP Bridge...", "system")
        try:
            if self.pending_plan:
                user_message = (
                    "Revise the pending plan using this user feedback.\n\n"
                    f"Pending plan:\n{self.pending_plan.model_dump_json()}\n\n"
                    f"User feedback:\n{user_message}"
                )
                self.pending_plan = None

            # 1. Ask bridge to assemble the Triage Agent & all Sub-Agents automatically
            # The Triage Agent is now the main entry point, and it includes the Planning Agent
            # as a specialist for complex task decomposition.
            triage_agent, sub_agents = await self.bridge.build_openai_system_via_fastmcp(
                model="o3-mini",
                triage_name="MainTriage",
                planner_name="Orchestrator",
                triage_instructions="For workflow requests, call create_workflow_plan and return its JSON exactly.",
                planner_model="o3-mini",
                planner_reasoning_effort="high",
                playbooks_dir="src/edma_mcp/skills/playbooks",
                enable_specialist_handoffs=False,
            )
            
            num_agents = len(sub_agents)
            self.log_system(f"Assembled Triage agent routing across {num_agents} agent(s), with Planner exposed as a tool.", "system")
            self.log_system("Generating or revising plan via OpenAI `Runner.run()`...", "system")

            # 2. Append new message to history
            self.history.append({"role": "user", "content": user_message})
            
            # 3. Let OpenAI Agents SDK handle everything!
            result = await Runner.run(
                starting_agent=triage_agent,
                input=self.history,
            )
            
            # 4. Store the updated history (includes agent responses and tool results)
            self.history = result.to_input_list()
            
            plan_review = self._extract_plan_review(result.final_output)
            if plan_review:
                self.pending_plan = plan_review
                self.log_system(self._render_plan_review(plan_review), "llm")
            else:
                self.log_system(f"{result.final_output}", "llm")
            
            if hasattr(result, 'tool_calls') and result.tool_calls:
                for t in result.tool_calls:
                    self.log_system(f"Agents framework called: {t.function.name}", "tool")

        except Exception as e:
            self.log_system(f"Agent Framework execution failed: {e}", "error")

    def start_listener(self):
        config = EventHubConfig(
            base_url="http://127.0.0.1:7300/event_hub/", 
            resource_base="events://hub/all",
            scope="all",
            poll_interval_sec=0.5
        )
        self.listener = EventHubListener(config=config, dispatcher=self.dispatcher)
        self.listener.start()

def main():
    root = tk.Tk()
    app = EDMAChatApp(root)
    # Ensure background thread logic gracefully shuts down on X click
    def on_closing():
        app.listener.stop()
        app.thread_helper.close()
        root.destroy()
        
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()

if __name__ == "__main__":
    main()
