import json
import os
from typing import Any, Dict, List, Optional, Tuple

from edma_mcp.client.bridge import MCPBridgeManager
from fastmcp.client import Client as FastMCPClient
from edma_mcp.client.orchestrator import OrchestratorUtils, get_orchestrator_instructions
from edma_mcp.client.models import TriageToPlanner, ExecuteStepInput, ExecuteStepOutput

# Attempt to import OpenAI-specific agent framework libraries
try:
    from agents import Agent, CodeInterpreterTool, ModelSettings
    from agents.tool import FunctionTool
    from agents.handoffs import handoff
    HAS_AGENTS_FRAMEWORK = True
except ImportError:
    HAS_AGENTS_FRAMEWORK = False


class OpenAIMCPBridge(MCPBridgeManager):
    """
    An extension of the standard MCPBridgeManager that automatically maps
    MCP agents, tools, and resources into an OpenAI Multi-Agent system.
    """

    async def build_openai_sub_agents_from_fastmcp(
        self,
        *,
        exclude_agents: Optional[List[str]] = None,
        include_resource_list_tool: bool = True,
        tool_name_style: str = "plain",
    ) -> Dict[str, "Agent"]:
        """
        Build one OpenAI Agent per MCP agent, using FastMCP bridge as transport.
        """
        if not HAS_AGENTS_FRAMEWORK:
            raise ImportError("The 'agents' library is not installed. Please install it using 'pip install openai-agents'.")

        if not self._agent_names:
            self.list_agents_sync()

        excluded = set(exclude_agents or [])
        sub_agents: Dict[str, Agent] = {}

        for agent_name in self._agent_names:
            if agent_name in excluded:
                continue

            tools = await self._build_openai_tools_for_one_agent(
                agent_name=agent_name,
                include_resource_list_tool=include_resource_list_tool,
                tool_name_style=tool_name_style,
            )
            
            # Optionally add code interpreter
            tools.append(CodeInterpreterTool(
                tool_config={
                    "type": "code_interpreter",
                    "container": {"type": "auto"},
                }
            ))
            
            instructions = await self._read_agent_prompt(agent_name)
            instructions += "\nany mathematical problem you should solve with the code interpretor tool."
            
            reasoning_str = await self._read_agent_reasoning_effort(agent_name)
            settings_kwargs = {}
            if reasoning_str:
                settings_kwargs["reasoning"] = {"effort": reasoning_str}
                
            kwargs: Dict[str, Any] = {
                "name": agent_name,
                "instructions": instructions,
                "tools": tools,
                "model": await self._read_agent_model(agent_name),
                "model_settings": ModelSettings(**settings_kwargs) if settings_kwargs else ModelSettings()
            }

            sub_agents[agent_name] = Agent(**kwargs)

        return sub_agents

    async def _build_openai_tools_for_one_agent(
        self,
        *,
        agent_name: str,
        include_resource_list_tool: bool,
        tool_name_style: str,
    ) -> List["FunctionTool"]:
        """
        Convert MCP tools (queried via FastMCP bridge) into OpenAI FunctionTool list.
        """
        bridge = await self._get_bridge(agent_name)

        mcp_tools = await bridge.list_tools()
        tool_specs: List[Dict[str, Any]] = self._normalize_mcp_tools(mcp_tools)

        openai_tools: List[FunctionTool] = []
        for spec in tool_specs:
            mcp_tool_name = str(spec.get("name", "")).strip()
            if not mcp_tool_name:
                continue

            if tool_name_style == "prefixed":
                openai_tool_name = f"{agent_name}__{mcp_tool_name}"
            else:
                openai_tool_name = mcp_tool_name

            desc = (spec.get("description") or "").strip()
            schema = spec.get("inputSchema") or spec.get("input_schema") or {}
            if not isinstance(schema, dict):
                schema = {}

            def sanitize_schema(s):
                if not isinstance(s, dict): return
                if s.get("type") == "object":
                    s.pop("additionalProperties", None)
                    for v in s.get("properties", {}).values():
                        sanitize_schema(v)
            
            sanitize_schema(schema)

            openai_tools.append(
                FunctionTool(
                    name=openai_tool_name,
                    description=desc or f"MCP tool '{mcp_tool_name}' from '{agent_name}'.",
                    params_json_schema=schema,
                    on_invoke_tool=self._make_fastmcp_tool_invoker(
                        agent_name=agent_name,
                        mcp_tool_name=mcp_tool_name,
                    ),
                )
            )

        if include_resource_list_tool:
            openai_tools.append(self._make_list_resources_tool_for_agent(agent_name))
            
            resource_tools = await self._build_resource_read_tools_for_agent(
                agent_name,
                max_tools=30,
                uri_prefix_allowlist=["nionswift://"], 
                max_chars=12000,
            )
            openai_tools.extend(resource_tools)

        return openai_tools

    def _make_fastmcp_tool_invoker(self, *, agent_name: str, mcp_tool_name: str):
        """
        Build OpenAI tool invoker that forwards calls to FastMCP bridge.call_tool().
        """
        async def _invoke(ctx, args_json: str) -> Any:
            args: Dict[str, Any] = {}
            if args_json:
                try:
                    args = json.loads(args_json)
                except Exception:
                    args = {}

            bridge = await self._get_bridge(agent_name)
            return await bridge.call_tool(mcp_tool_name, args)

        return _invoke

    def _make_list_resources_tool_for_agent(self, agent_name: str) -> "FunctionTool":
        """
        A per-agent extra tool to list resources, useful for LLM to discover state.
        """
        async def _invoke(ctx, args_json: str) -> Any:
            bridge = await self._get_bridge(agent_name)
            resources = await bridge.list_resources()
            return {"agent_name": agent_name, "resources": resources}

        return FunctionTool(
            name=f"{agent_name}__mcp_list_resources",
            description=f"List MCP resources exposed by agent '{agent_name}'.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=_invoke,
        )

    async def _build_resource_read_tools_for_agent(
        self,
        agent_name: str,
        *,
        max_tools: int = 30,
        uri_prefix_allowlist: Optional[List[str]] = None,
        max_chars: int = 12000,
    ) -> List["FunctionTool"]:
        """
        Create one FunctionTool per resource URI.
        """
        bridge = await self._get_bridge(agent_name)
        resources = await bridge.list_resources()
        entries = self._extract_resource_name_uri(resources)
        
        if uri_prefix_allowlist:
            entries = [
                (res_name, uri)
                for (res_name, uri) in entries
                if any(uri.startswith(p) for p in uri_prefix_allowlist)
            ]

        entries = entries[:max_tools]
        tools: List[FunctionTool] = []
        for res_name, uri in entries:
            tool_name = f"{agent_name}__read_resource__{res_name}"

            async def _invoke(ctx, args_json: str, _uri=uri):
                b = await self._get_bridge(agent_name)
                res = await b.read_resource(_uri)
                text = self._extract_resource_text(res)
                if isinstance(text, str) and len(text) > max_chars:
                    text = text[:max_chars] + "\n...[truncated]..."

                return {"agent_name": agent_name, "uri": _uri, "text": text}
                
            tools.append(
                FunctionTool(
                    name=tool_name,
                    description=f"Read MCP resource '{uri}' from agent '{agent_name}'. About the resource: {res_name}",
                    params_json_schema={"type": "object", "properties": {}},
                    on_invoke_tool=_invoke,
                )
            )

        return tools

    def _extract_resource_name_uri(self, resources):
        result = []
        if not resources:
            return result
        for r in resources:
            name = getattr(r, "name", None)
            uri = getattr(r, "uri", None)
            if name is None or uri is None:
                continue
            try:
                uri_str = str(uri)
                result.append((str(name), uri_str))
            except Exception:
                continue
        return result

    def _extract_resource_text(self, res: Any) -> str:
        if res is None:
            return ""
        if isinstance(res, list) and res:
            first = res[0]
            text = getattr(first, "text", None)
            if isinstance(text, str):
                return text
        text = getattr(res, "text", None)
        if isinstance(text, str):
            return text
        try:
            return str(res)
        except Exception:
            return ""

    def _normalize_mcp_tools(self, mcp_tools: Any) -> List[Dict[str, Any]]:
        if not mcp_tools:
            return []
        if isinstance(mcp_tools, list):
            out: List[Dict[str, Any]] = []
            for t in mcp_tools:
                if isinstance(t, dict):
                    out.append(t)
                else:
                    name = getattr(t, "name", None)
                    desc = getattr(t, "description", None)
                    schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
                    d: Dict[str, Any] = {}
                    if isinstance(name, str):
                        d["name"] = name
                    if isinstance(desc, str):
                        d["description"] = desc
                    if isinstance(schema, dict):
                        d["inputSchema"] = schema
                    out.append(d)
            return out
        return []

    async def _read_agent_introduction(self, agent_name: str) -> str:
        bridge = await self._get_bridge(agent_name)
        uri = f"introduction://{agent_name}"
        res = await bridge.read_resource(uri)
        text = self._extract_resource_text(res)
        return text.strip() if isinstance(text, str) else ""

    async def _read_agent_prompt(self, agent_name: str) -> str:
        bridge = await self._get_bridge(agent_name)
        uri = f"prompt://{agent_name}"
        res = await bridge.read_resource(uri)
        text = self._extract_resource_text(res)
        return text.strip() if isinstance(text, str) else ""

    async def _read_agent_model(self, agent_name: str) -> str:
        bridge = await self._get_bridge(agent_name)
        uri = f"model://{agent_name}"
        res = await bridge.read_resource(uri)
        text = self._extract_resource_text(res)
        return text.strip() if isinstance(text, str) else ""

    async def _read_agent_reasoning_effort(self, agent_name: str) -> str:
        bridge = await self._get_bridge(agent_name)
        uri = f"reasoning://{agent_name}"
        res = await bridge.read_resource(uri)
        text = self._extract_resource_text(res)
        return text.strip() if isinstance(text, str) else ""

    async def _generate_agents_registry_summary(self, sub_agents: Dict[str, "Agent"], detailed: bool = True) -> str:
        """
        Consolidate all agents' metadata into a single string for the Planning Agent's context.
        If detailed=False, only agent names and intros are provided (forcing skill-reading).
        """
        summary = "AVAILABLE AGENTS AND THEIR CAPABILITIES:\n\n"
        for agent_name in sub_agents.keys():
            intro = await self._read_agent_introduction(agent_name)
            summary += f"--- Agent: {agent_name} ---\n"
            summary += f"Description: {intro}\n"
            
            if detailed:
                try:
                    bridge = await self._get_bridge(agent_name)
                    mcp_tools = await bridge.list_tools()
                    tool_specs = self._normalize_mcp_tools(mcp_tools)
                    
                    if tool_specs:
                        summary += "Tools / Functions:\n"
                        for spec in tool_specs:
                            name = spec.get("name")
                            desc = (spec.get("description") or "No description").strip()
                            summary += f"  - {name}: {desc}\n"
                    else:
                        summary += "Tools: None\n"
                except Exception as e:
                    summary += f"Tools: (Error fetching tools: {e})\n"
            else:
                summary += "Note: Specific technical functions for this agent are hidden. You MUST read the relevant SKILL playbook to find the correct function call instructions.\n"
            
            summary += "\n"
        return summary

    async def build_planning_agent(
        self,
        sub_agents: Dict[str, "Agent"],
        *,
        model=None,
        reasoning_effort: Optional[str] = "high",
        planning_instructions: Optional[str] = None,
        planning_name: str = "planning_orchestrator",
    ) -> "Agent":
        """
        Build a planning agent with knowledge of all specialist agents and their tools.
        Note: The triage_agent handoff will be added after both agents are created to support circularity.
        """
        registry_summary = await self._generate_agents_registry_summary(sub_agents)
        
        instructions = (
            f"{planning_instructions or ''}\n"
            "You are the Workflow Planning Agent. Your primary role is to coordinate complex tasks.\n"
            "1. REQUISITE KNOWLEDGE:\n"
            f"{registry_summary}\n"
            "2. YOUR PROCESS:\n"
            "   - When a user makes a request, analyze it and break it down into a sequence of high-level steps.\n"
            "   - For each step, determine which specialist agent is best suited to execute it.\n"
            "   - You are also allowed to consult or delegate planning sub-tasks DIRECTLY to specialist agents if needed.\n"
            "   - IMPORTANT CONSULTATION RULE: When consulting a specialist, your query MUST clearly state: 'This request is ONLY for planning purposes. Do NOT execute any concrete actions yet. Once you provide the detailed workflow, you MUST use the handoff tool to return control to the Planning Agent.'\n"
            "   - Once the high-level plan is ready, you MUST ACTUALLY CALL the handoff tool to transfer back to the Triage Agent.\n"
            "   - DO NOT just state that you are handing off; you MUST invoke the tool.\n"
            "   - Once the Triage Agent returns the detailed workflows for each step, aggregate them into a single final workflow for the user.\n"
            "3. HANDOFF ENFORCEMENT:\n"
            "   - To send the plan back, you MUST use the handoff tool for the Triage Agent.\n"
        )

        settings_kwargs = {}
        if reasoning_effort:
            settings_kwargs["reasoning"] = {"effort": reasoning_effort}

        return Agent(
            name=planning_name,
            instructions=instructions,
            handoffs=[], # Will be populated in build_openai_system_via_fastmcp
            model=model,
            model_settings=ModelSettings(**settings_kwargs) if settings_kwargs else ModelSettings()
        )

    async def build_triage_agent(
        self,
        sub_agents: Dict[str, "Agent"],
        *,
        model=None,
        reasoning_effort: Optional[str] = "high",
        triage_instructions=None,
        triage_name: str = "triage",
    ) -> "Agent":
        """
        Build a triage agent whose handoff descriptions
        are automatically derived from MCP introduction resources.
        """
        handoffs = []

        for name, ag in sub_agents.items():
            # Only attempt to read MCP metadata if the agent was discovered via MCP
            intro = None
            if name in self._agent_names:
                try:
                    intro = await self._read_agent_introduction(name)
                except Exception:
                    # If reading fails, just fallback to default description
                    pass

            if intro:
                desc = f"Specialist agent '{name}'.\n{intro}"
            else:
                desc = f"Specialist agent '{name}'."

            handoffs.append(
                handoff(
                    agent=ag,
                    tool_description_override=desc,
                )
            )

        instructions = (
            f"{triage_instructions or ''}\n"
            "You are a triage agent and coordination specialist.\n"
            "1. ROUTING DECISION:\n"
            "   - If a request is simple and requires only ONE specialist, hand off DIRECTLY to that agent.\n"
            "   - If a request is complex and involves MULTIPLE agents or steps, hand off to the Planning Agent (Orchestrator).\n"
            "2. HANDOFF ENFORCEMENT:\n"
            "   - You MUST ACTUALLY CALL the handoff tool for the chosen agent. Never just describe the action in text.\n"
            "3. WORKFLOW COORDINATION (From Planning Agent):\n"
            "   - If you receive a multi-step plan from the Planning Agent, iterate through each step.\n"
            "   - For each step, contact the relevant specialist agent and ask: 'Provide a detailed technical workflow for this step: [step description]'.\n"
            "   - COLLECT AND SUMMARIZE these detailed responses.\n"
            "   - Once all steps are detailed, you MUST ACTUALLY CALL the handoff tool to return the aggregate to the Planning Agent.\n"
            "4. IMPORTANT:\n"
            "   - Always hand off to specialists to get their specific workflow details.\n"
            "   - Do not make up technical details for specialists yourself."
        )

        settings_kwargs = {}
        if reasoning_effort:
            settings_kwargs["reasoning"] = {"effort": reasoning_effort}

        return Agent(
            name=triage_name,
            instructions=instructions,
            handoffs=handoffs,
            model=model,
            model_settings=ModelSettings(**settings_kwargs) if settings_kwargs else ModelSettings()
        )

    async def build_openai_system_via_fastmcp(
        self,
        *,
        model: Optional[Any] = None,
        exclude_agents: Optional[List[str]] = None,
        include_resource_list_tool: bool = True,
        tool_name_style: str = "plain",
        triage_name: str = "MainTriage",
        triage_instructions: Optional[str] = None,
        triage_reasoning_effort: Optional[str] = "high",
        orchestrator_name: str = "Planner",
        orchestrator_model: Optional[Any] = None,
        orchestrator_reasoning_effort: Optional[str] = "high",
        executive_name: str = "Executive",
        playbooks_dir: str = "src/edma_mcp/skills/playbooks",
    ) -> Tuple["Agent", Dict[str, "Agent"]]:
        """
        Build a multi-agent system including:
          - Triage (Entry point)
          - Planner (Orchestrator, reads Playbooks, outputs ExecuteStepInput)
          - Executive (Forwarder, connects Planner to Specialists)
          - Specialists (MCP Agents)
        """
        from edma_mcp.client.models import TriageToPlanner, ExecuteStepInput, ExecuteStepOutput, Plan

        # 1. Build Specialist Agents from MCP
        sub_agents = await self.build_openai_sub_agents_from_fastmcp(
            exclude_agents=exclude_agents,
            include_resource_list_tool=include_resource_list_tool,
            tool_name_style=tool_name_style,
        )

        # 2. Build Planner (Orchestrator)
        import edma_mcp
        package_root = os.path.dirname(os.path.dirname(edma_mcp.__file__))
        abs_playbooks_dir = os.path.join(package_root, "edma_mcp", "skills", "playbooks")
        
        registry_summary = await self._generate_agents_registry_summary(sub_agents, detailed=False)
        orchestrator_instructions = get_orchestrator_instructions(abs_playbooks_dir, registry_summary)
        
        settings_kwargs = {}
        if orchestrator_reasoning_effort:
            settings_kwargs["reasoning"] = {"effort": orchestrator_reasoning_effort}
            
        async def read_skill_tool(ctx, args_json: str) -> str:
            args = json.loads(args_json) if args_json else {}
            skill_id = args.get("skill_id")
            return OrchestratorUtils.read_skill_content(abs_playbooks_dir, skill_id) if skill_id else "Error: skill_id is required."
            
        async def list_skills_tool(ctx, args_json: str) -> str:
            return OrchestratorUtils.get_skills_catalog(abs_playbooks_dir)

        planner = Agent(
            name=orchestrator_name,
            instructions=orchestrator_instructions,
            model=orchestrator_model or model,
            model_settings=ModelSettings(**settings_kwargs) if settings_kwargs else ModelSettings(),
            tools=[
                FunctionTool(
                    name="list_skills",
                    description="List all available high-level skills (Playbooks).",
                    on_invoke_tool=list_skills_tool,
                    params_json_schema={"type": "object", "properties": {}}
                ),
                FunctionTool(
                    name="read_skill_content",
                    description="Read the detailed step-by-step instructions for a specific skill.",
                    on_invoke_tool=read_skill_tool,
                    params_json_schema={
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "The ID of the skill to read."}
                        },
                        "required": ["skill_id"]
                    }
                )
            ],
            handoffs=[]
        )

        # 3. Build Executive (Autonomous Loop Manager)
        executive_instructions = (
            "You are the Autonomous Executive (Workflow Manager).\n"
            "1. ACCESS PLAN: You receive the full workflow plan within the shared context (WorkflowState).\n"
            "2. LOOP EXECUTION: Based on the 'current_step_index' in the context, identify the next Specialist agent to call.\n"
            "3. FORWARD: Use the corresponding handoff tool to the specialist. Provide precisely the 'input' and 'goal' specified in the plan.\n"
            "4. ANALYZE & PROGRESS: When the specialist hands back to you, evaluate the 'on_success' or 'on_failure' instructions from the plan. Increment the step index or jump to fallback steps as needed.\n"
            "5. COMPLETION: Once the plan reaches 'done' or 'stop', hand off back to the Triage Agent to present the final report.\n"
            "RULE: You are the autonomous driver. Do NOT ask for user input. Your only handoff targets are Specialists and the Triage Agent."
        )
        executive = Agent(
            name=executive_name,
            instructions=executive_instructions,
            model=model,
            handoffs=[]
        )

        # 4. Build Triage (The Entry & Review Hub)
        triage_instructions = (
            f"{triage_instructions or ''}\n"
            "You are the Main Triage and Approval Hub.\n"
            "1. PLANNING: For complex tasks, hand off to the Planner to generate a 'Plan'.\n"
            "2. REVIEW: When the Planner hands back a plan, present it clearly to the user in Markdown (table/list) and wait for confirmation.\n"
            "3. START: Once the user explicitly confirms (e.g. 'go', 'execute'), hand off to the Executive agent to start autonomous processing.\n"
            "4. REPORT: Receive final reports from the Executive and present them to the user."
        )
        triage = await self.build_triage_agent(
            {}, 
            model=model,
            reasoning_effort=triage_reasoning_effort,
            triage_name=triage_name,
            triage_instructions=triage_instructions,
        )

        # 5. Connect Handoffs (New Autonomous Pipeline)
        
        # 5a. Triage -> Planner OR Executive
        triage.handoffs = [
            handoff(
                agent=planner,
                input_type=TriageToPlanner,
                on_handoff=lambda ctx, data: planner,
                tool_description_override="Hand off complex requests to the Planner for deep skill analysis and workflow creation."
            ),
            handoff(
                agent=executive,
                input_type=Plan,
                on_handoff=lambda ctx, data: executive,
                tool_description_override="Launch autonomous execution of the confirmed plan using the Executive Agent."
            )
        ]
        
        # 5b. Planner -> Triage (Returns for confirmation)
        planner.handoffs = [
            handoff(
                agent=triage,
                input_type=Plan,
                on_handoff=lambda ctx, data: triage,
                tool_description_override="Return the completed plan to Triage for user review and approval."
            )
        ]
        
        # 5c. Executive -> Specialists OR Triage (Loops specialists, returns to Triage at end)
        executive_handoffs = [
            handoff(
                agent=triage,
                tool_description_override="Provide the final summary of the completed workflow back to Triage."
            )
        ]
        for ag in sub_agents.values():
            executive_handoffs.append(handoff(agent=ag))
        executive.handoffs = executive_handoffs

        # 5d. Specialists -> Executive (The loop-back)
        for ag in sub_agents.values():
            ag.handoffs = [handoff(agent=executive)]

        all_specialists = dict(sub_agents)
        all_specialists[orchestrator_name] = planner
        all_specialists[executive_name] = executive

        return triage, all_specialists
