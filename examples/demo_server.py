import tkinter as tk
import threading
import asyncio
from typing import Optional

from edma_mcp.server.base import BaseMCP, mcp_tool
from edma_mcp.server.server import EDMAServer
from edma_mcp.server.event import EventMCP

# 1. Provide a Search Agent
class SearchAgent(BaseMCP):
    def __init__(self, event_mcp: Optional[EventMCP] = None):
        super().__init__(
            name="search_agent",
            introduction="I provide search information for different cities.",
            prompt="You are a search assistant.",
            model="gpt-4o",
            event_mcp=event_mcp
        )

    @mcp_tool()
    def search_weather(self, location: str) -> str:
        """Search weather and basic info for a given location."""
        info = {
            "New York": "20°C. Weather: Sunny. Famous for: Statue of Liberty.",
            "Berlin": "15°C. Weather: Cloudy. Famous for: Brandenburg Gate.",
            "Tokyo": "25°C. Weather: Clear. Famous for: Tokyo Tower."
        }.get(location, f"No specific information found for {location}.")
        return info


# 2. Provide an FFT Agent
class FFTAgent(BaseMCP):
    def __init__(self, event_mcp: Optional[EventMCP] = None):
        super().__init__(
            name="fft_agent",
            introduction="I can perform 2D FFT on uploaded images.",
            prompt="You process images.",
            model="gpt-4o",
            event_mcp=event_mcp
        )
        
    @mcp_tool()
    def perform_fft(self, image_path: str) -> str:
        """Perform 2D Fast Fourier Transform on the input image."""
        # For demo purposes, we do not require actual CV packages here, 
        # we just return a simulated success message when the LLM triggers it.
        return f"FFT processed successfully for image at: {image_path}. Saved as /tmp/fft_result.png"


class EDMAServerUI:
    def __init__(self, root):
        self.root = root
        self.root.title("EDMA Server UI (Agent Apps)")
        self.root.geometry("550x350")
        
        # 1. Initialize Server & Agents
        self.server = EDMAServer(host="127.0.0.1", port=7300)
        self.search_agent = SearchAgent(event_mcp=self.server.event_hub)
        self.fft_agent = FFTAgent(event_mcp=self.server.event_hub)
        
        self.server.add_agent(self.search_agent)
        self.server.add_agent(self.fft_agent)

        # 2. Run the actual UVICORN server quietly in the background
        threading.Thread(target=self.server.run, daemon=True).start()

        # 3. Build UI
        lbl = tk.Label(root, text="EDMA App GUI - Publish Events to LLM Client", font=("Arial", 14, "bold"))
        lbl.pack(pady=10)

        # ---- Search Agent GUI Panel ----
        frame_search = tk.LabelFrame(root, text="Agent 1: Search Manager", padx=10, pady=10)
        frame_search.pack(fill="x", padx=10, pady=5)
        
        btn_search_show = tk.Button(frame_search, text="Send 'Show' Event (Update UI/Status)", 
                                    command=lambda: self.push_event_to_hub(
                                        self.search_agent, "show", 
                                        {"message": "User just opened the Search Manager map!"}))
        btn_search_show.pack(side="left", padx=5)
        
        btn_search_trig = tk.Button(frame_search, text="Send 'Trigger' (Ask LLM to Search)", 
                                    command=lambda: self.push_event_to_hub(
                                        self.search_agent, "trigger", 
                                        {"tool_call_message": {"trigger_message": "Please use search_weather tool to look up Tokyo."}}))
        btn_search_trig.pack(side="left", padx=5)

        # ---- FFT Agent GUI Panel ----
        frame_fft = tk.LabelFrame(root, text="Agent 2: FFT Image Processor", padx=10, pady=10)
        frame_fft.pack(fill="x", padx=10, pady=5)

        btn_fft_show = tk.Button(frame_fft, text="Send 'Show' Event (Select File)", 
                                 command=lambda: self.push_event_to_hub(
                                     self.fft_agent, "show", 
                                     {"message": "User selected a new image and wants you to see it."}))
        btn_fft_show.pack(side="left", padx=5)

        btn_fft_trig = tk.Button(frame_fft, text="Send 'Trigger' (Process Image)", 
                                 command=lambda: self.push_event_to_hub(
                                     self.fft_agent, "trigger", 
                                     {"tool_call_message": {"trigger_message": "Please use perform_fft tool to process the newly uploaded /tmp/test.png image."}}))
        btn_fft_trig.pack(side="left", padx=5)
        
    def push_event_to_hub(self, agent: BaseMCP, e_type: str, payload: dict):
        """
        Creates an asyncio loop in a separate thread to safely await the push_event method.
        This signals the EventHub to broadcast SSE to the client.
        """
        def _bg_push():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    agent.push_event(event_type=e_type, payload=payload, targets=[e_type])
                )
                print(f"[Server UI Emit] Successfully pushed '{e_type}' event for '{agent.name}'.")
            except Exception as e:
                print(f"[Server UI Emit] Failed to push event: {e}")
            finally:
                loop.close()
                
        threading.Thread(target=_bg_push, daemon=True).start()


def main():
    root = tk.Tk()
    app = EDMAServerUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
