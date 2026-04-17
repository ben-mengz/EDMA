import tkinter as tk
import threading
import asyncio
from typing import Optional, Dict, Any

from edma_mcp.server.base import BaseMCP, mcp_tool
from edma_mcp.server.server import EDMAServer
from edma_mcp.server.event import EventMCP

class SEMAgent(BaseMCP):
    def __init__(self, event_mcp: Optional[EventMCP] = None):
        super().__init__(
            name="sem_agent",
            introduction="I manage SEM image and 4D-STEM data acquisition.",
            prompt="You are a microscope control assistant specializing in SEM.",
            model="gpt-4o",
            event_mcp=event_mcp
        )

    @mcp_tool()
    def turn_on_inlens_detector(self) -> str:
        """Turn on the inLens detector."""
        return "inLens detector is now ACTIVE."

    @mcp_tool()
    def confirm_roi(self, roi_description: str) -> str:
        """Confirm the Region of Interest for the upcoming scan."""
        return f"ROI confirmed: {roi_description}"

    @mcp_tool()
    def set_defocus(self, val: float) -> str:
        """Set the beam defocus value."""
        return f"Defocus set to {val} nm."

    @mcp_tool()
    def acquire_4d_stem_data(self) -> str:
        """Execute the 4D-STEM acquisition sequence."""
        return "4D-STEM data acquired successfully. Saved to /tmp/4d_stem_data.h5"

class PtychographyAgent(BaseMCP):
    def __init__(self, event_mcp: Optional[EventMCP] = None):
        super().__init__(
            name="ptychography_agent",
            introduction="I handle ptychography reconstruction workflows and parameters.",
            prompt="You are a ptychography reconstruction specialist.",
            model="gpt-4o",
            event_mcp=event_mcp
        )

    @mcp_tool()
    def check_parameters(self, params_json: str) -> str:
        """Check if all essential ptychography parameters (in JSON string) are valid."""
        return f"Parameters checked: {params_json}. Status: All essential fields present."

    @mcp_tool()
    def append_parameter(self, key: str, value: str) -> str:
        """Append or update a ptychography parameter (ensure value is a string)."""
        return f"Parameter '{key}' set to '{value}'."

    @mcp_tool()
    def save_parameters(self) -> str:
        """Save the current set of reconstruction parameters."""
        return "Parameters saved to /tmp/ptychography_params.json"

    @mcp_tool()
    def send_to_server(self) -> str:
        """Send parameters and data to the reconstruction server."""
        return "Ptychography job successfully submitted to the server."


class MicroscopeServerUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Microscope Server (SEM + Ptychography)")
        self.root.geometry("600x400")
        
        # 1. Initialize Server & Agents
        self.server = EDMAServer(host="127.0.0.1", port=7300)
        self.sem_agent = SEMAgent(event_mcp=self.server.event_hub)
        self.ptychography_agent = PtychographyAgent(event_mcp=self.server.event_hub)
        
        self.server.add_agent(self.sem_agent)
        self.server.add_agent(self.ptychography_agent)

        # 2. Run the actual UVICORN server in the background
        threading.Thread(target=self.server.run, daemon=True).start()

        # 3. Build UI
        tk.Label(root, text="Microscope Control Server", font=("Arial", 16, "bold")).pack(pady=10)
        
        frame_sem = tk.LabelFrame(root, text="SEM Control", padx=10, pady=10)
        frame_sem.pack(fill="x", padx=10, pady=5)
        
        tk.Button(frame_sem, text="Trigger 4D-STEM Acquisition", 
                  command=lambda: self.push_event_to_hub(
                      self.sem_agent, "trigger", 
                      {"tool_call_message": {"trigger_message": "Please perform 4D-STEM acquisition workflow."}})).pack()

        frame_ptych = tk.LabelFrame(root, text="Ptychography Engine", padx=10, pady=10)
        frame_ptych.pack(fill="x", padx=10, pady=5)
        
        tk.Button(frame_ptych, text="Trigger Ptychography Processing", 
                  command=lambda: self.push_event_to_hub(
                      self.ptychography_agent, "trigger", 
                      {"tool_call_message": {"trigger_message": "Please start the ptychography reconstruction process."}})).pack()

    def push_event_to_hub(self, agent: BaseMCP, e_type: str, payload: dict):
        def _bg_push():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    agent.push_event(event_type=e_type, payload=payload, targets=[e_type])
                )
            finally:
                loop.close()
        threading.Thread(target=_bg_push, daemon=True).start()

def main():
    root = tk.Tk()
    app = MicroscopeServerUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
