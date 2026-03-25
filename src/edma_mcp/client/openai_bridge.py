import json
from typing import Any, Dict, List, Optional, Tuple

from edma_mcp.client.bridge import MCPBridgeManager
from fastmcp.client import Client as FastMCPClient

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

    async def _generate_agents_registry_summary(self, sub_agents: Dict[str, "Agent"]) -> str:
        """
        Consolidate all agents' metadata (introduction and tool descriptions)
        into a single string for the Planning Agent's context.
        """
        summary = "AVAILABLE AGENTS AND THEIR CAPABILITIES:\n\n"
        for agent_name in sub_agents.keys():
            intro = await self._read_agent_introduction(agent_name)
            summary += f"--- Agent: {agent_name} ---\n"
            summary += f"Description: {intro}\n"
            
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
        triage_name: str = "triage",
        triage_instructions: Optional[str] = None,
        triage_reasoning_effort: Optional[str] = "high",
        planning_name: str = "planning",
        planning_instructions: Optional[str] = None,
        planning_model: Optional[Any] = None,
        planning_reasoning_effort: Optional[str] = "high",
    ) -> Tuple["Agent", Dict[str, "Agent"]]:
        """
        One-call builder:
          - discovers agents if needed
          - builds sub-agents (FastMCP transport)
          - builds planning agent (orchestrator)
          - builds triage agent (starting point)
        Returns (triage_agent, all_sub_agents_including_planning)
        """
        # 1. Build basic specialist agents
        sub_agents = await self.build_openai_sub_agents_from_fastmcp(
            exclude_agents=exclude_agents,
            include_resource_list_tool=include_resource_list_tool,
            tool_name_style=tool_name_style,
        )

        # 2. Build planning agent (it needs to know about sub_agents)
        planning = await self.build_planning_agent(
            sub_agents,
            model=planning_model or model,
            reasoning_effort=planning_reasoning_effort,
            planning_name=planning_name,
            planning_instructions=planning_instructions,
        )

        # 3. Add planning agent to the pool of sub-agents for triage
        all_specialists = dict(sub_agents)
        all_specialists[planning_name] = planning

        # 4. Build triage agent with all sub-agents (including planning)
        triage = await self.build_triage_agent(
            all_specialists,
            model=model,
            reasoning_effort=triage_reasoning_effort,
            triage_name=triage_name,
            triage_instructions=triage_instructions,
        )

        # 5. Connect planning to triage and all specialist agents
        # This allows the planning agent to consult specialists while making the plan.
        handoffs_for_planning = [handoff(agent=triage)]
        for name, ag in sub_agents.items():
            handoffs_for_planning.append(handoff(agent=ag))
        
        planning.handoffs = handoffs_for_planning

        # 6. Add return handoffs to all specialist sub-agents
        # This allows specialists to explicitly return control to the orchestrators.
        for name, ag in sub_agents.items():
            if not ag.handoffs:
                ag.handoffs = []
            ag.handoffs.append(handoff(agent=triage))
            ag.handoffs.append(handoff(agent=planning))

        return triage, all_specialists
