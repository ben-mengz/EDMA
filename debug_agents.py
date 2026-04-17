import asyncio
import os
import sys

# Ensure src is in path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from edma_mcp.client.openai_bridge import OpenAIMCPBridge

async def main():
    # Setup dummy environment
    os.environ['OPENAI_API_KEY'] = 'sk-dummy'
    
    bridge = OpenAIMCPBridge(base_http_url="http://localhost:7300")
    print("Building agent system...")
    try:
        triage, agents = await bridge.build_openai_system_via_fastmcp()
        
        planner = agents.get('Planner')
        if not planner:
            print(f"ERROR: Planner agent not found. Available keys: {list(agents.keys())}")
            return

        print(f"\n--- Agent: {planner.name} ---")
        
        # 1. Check direct raw handoffs
        print(f"Number of Handoffs: {len(planner.handoffs)}")
        for i, h in enumerate(planner.handoffs):
            # Print all attrs to find the target
            print(f"Handoff {i} attrs: {dir(h)}")
            # Try some common attr names
            for attr in ['agent', 'target_agent', 'agent_name', '_agent']:
                if hasattr(h, attr):
                    val = getattr(h, attr)
                    print(f"  Found {attr}: {val.name if hasattr(val, 'name') else val}")

        # 2. Check ACTUAL TOOLS (This is what OpenAI sees!)
        print(f"\nActual tools exposed to OpenAI:")
        for t in planner.tools:
            print(f"  - Tool: {t.name} ({t.description[:50]}...)")
            
    except Exception as e:
        print(f"Exception during build: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
