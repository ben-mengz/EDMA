import json
import os
from typing import Any, Dict, List, Optional, Tuple

from edma_mcp.client.bridge import MCPBridgeManager
from fastmcp.client import Client as FastMCPClient
from edma_mcp.client.orchestrator import OrchestratorUtils, get_orchestrator_instructions
from edma_mcp.client.models import PlanReview

# Attempt to import OpenAI-specific agent framework libraries
try:
    from agents import Agent, CodeInterpreterTool, ModelSettings
    from agents.agent_output import AgentOutputSchema
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

    def _build_reasoning_settings(
        self,
        reasoning_effort: Optional[str] = None,
        reasoning_summary: Optional[str] = "auto",
    ) -> Dict[str, Any]:
        settings_kwargs: Dict[str, Any] = {}
        reasoning: Dict[str, Any] = {}
        if reasoning_effort:
            reasoning["effort"] = reasoning_effort
        if reasoning_summary:
            reasoning["summary"] = reasoning_summary
        if reasoning:
            settings_kwargs["reasoning"] = reasoning
        return settings_kwargs

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
            instructions += (
                "\nany mathematical problem you should solve with the code interpretor tool."
                "\nIf an MCP tool returns JSON with ok=false or an error field, treat the tool call as failed. "
                "Report the failing tool name and exact error message back to the caller. Do not claim success."
            )
            
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
                except Exception as exc:
                    return self._format_tool_error(
                        agent_name=agent_name,
                        tool_name=mcp_tool_name,
                        error_type=type(exc).__name__,
                        message=f"Invalid JSON arguments: {exc}",
                        arguments_raw=args_json,
                    )

            try:
                bridge = await self._get_bridge(agent_name)
                return await bridge.call_tool(mcp_tool_name, args)
            except Exception as exc:
                return self._format_tool_error(
                    agent_name=agent_name,
                    tool_name=mcp_tool_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    arguments=args,
                )

        return _invoke

    def _format_tool_error(
        self,
        *,
        agent_name: str,
        tool_name: str,
        error_type: str,
        message: str,
        arguments: Optional[Dict[str, Any]] = None,
        arguments_raw: Optional[str] = None,
    ) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "agent": agent_name,
                    "tool": tool_name,
                    "type": error_type,
                    "message": message,
                    "arguments": arguments if arguments is not None else None,
                    "arguments_raw": arguments_raw,
                },
                "instruction_to_llm": (
                    "The MCP tool failed. Do not pretend it succeeded. "
                    "Tell the user which tool failed and include the error message. "
                    "If recoverable, ask for the specific missing input or propose the next recovery step."
                ),
            },
            ensure_ascii=False,
            default=str,
        )

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
                try:
                    b = await self._get_bridge(agent_name)
                    res = await b.read_resource(_uri)
                    text = self._extract_resource_text(res)
                    if isinstance(text, str) and len(text) > max_chars:
                        text = text[:max_chars] + "\n...[truncated]..."

                    return {"agent_name": agent_name, "uri": _uri, "text": text}
                except Exception as exc:
                    return self._format_tool_error(
                        agent_name=agent_name,
                        tool_name=tool_name,
                        error_type=type(exc).__name__,
                        message=f"Error reading resource '{_uri}': {exc}",
                    )
                
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
        contents = getattr(res, "contents", None)
        if isinstance(contents, list) and contents:
            first = contents[0]
            text = getattr(first, "text", None)
            if isinstance(text, str):
                return text
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

    def _resolve_playbooks_dir(self, playbooks_dir: Optional[str] = None) -> str:
        if playbooks_dir:
            return os.path.abspath(playbooks_dir)
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(package_root, "skills", "playbooks")

    async def build_planner_agent(
        self,
        sub_agents: Dict[str, "Agent"],
        *,
        model=None,
        reasoning_effort: Optional[str] = "high",
        planner_instructions: Optional[str] = None,
        planner_name: str = "Planner",
        playbooks_dir: Optional[str] = None,
    ) -> "Agent":
        """
        Build a planning agent that reads skills and returns a PlanReview.
        This agent is used as a tool by Triage, not as a handoff target.
        """
        abs_playbooks_dir = self._resolve_playbooks_dir(playbooks_dir)
        registry_summary = await self._generate_agents_registry_summary(sub_agents, detailed=True)
        skill_instructions = get_orchestrator_instructions(abs_playbooks_dir, registry_summary)
        self.planner_discovery_log = []

        async def list_skills_tool(ctx, args_json: str) -> str:
            self.planner_discovery_log = ["list_skills"]
            return OrchestratorUtils.get_skills_catalog(abs_playbooks_dir)

        async def read_skill_tool(ctx, args_json: str) -> str:
            args = json.loads(args_json) if args_json else {}
            skill_id = args.get("skill_id")
            if not skill_id:
                return "Error: skill_id is required."
            self.planner_discovery_log.append(f"read_skill_content:{skill_id}")
            return OrchestratorUtils.read_skill_content(abs_playbooks_dir, str(skill_id))

        async def list_agents_capabilities_tool(ctx, args_json: str) -> str:
            self.planner_discovery_log.append("list_agents_capabilities")
            return registry_summary

        instructions = (
            f"{planner_instructions or ''}\n\n"
            f"{skill_instructions}\n\n"
            "CRITICAL OUTPUT RULES:\n"
            "- Return a PlanReview object only. Do not execute MCP tools.\n"
            "- Every step must name an existing agent and exact MCP tool_name.\n"
            "- Every step arguments field must be a JSON object, even when empty.\n"
            "- Read the relevant skill before choosing tool names.\n"
            "- Treat the selected skill as the workflow contract. Plan steps must come from the skill's ## Steps and ## Required Tools.\n"
            "- If the selected skill references another skill/playbook, call read_skill_content for every referenced skill and expand those concrete steps into the plan.\n"
            "- Do not invent bridge/check steps between compound-skill sections unless a referenced skill explicitly lists them.\n"
            "- Do not add steps or tools merely because they appear in list_agents_capabilities. Agent capabilities are for validation only.\n"
            "- If the user's request needs a step/tool that is not in the skill, put that limitation in risks instead of adding an unsourced step.\n"
            "- Do not ask for all missing inputs during planning.\n"
            "- Leave PlanReview.missing_inputs empty unless no executable draft plan can be made at all.\n"
            "- Put step-specific missing inputs in that step's required_inputs list and set on_failure to ask_user when appropriate.\n"
            "- If an argument will be known only after a previous step, use a clear placeholder such as {{step_3.data_path}} and explain it in required_inputs only if the user must provide it.\n"
            "- Do not continue an old blocked execution step or old PlanReview from conversation history unless the latest user message explicitly says to continue/resume/execute it or directly provides that step's requested inputs.\n"
            "- For a separate latest-message goal, create a fresh PlanReview starting at step_id 1.\n"
            "- If the input includes UI action context/recent actions/tool trigger outputs, use them to infer completed skill steps and omit completed steps from the new PlanReview for that same workflow.\n"
            "- Do not repeat completed tool calls shown in UI action context unless the user explicitly asks to redo them.\n"
            "- Structured tool status is authoritative: success=completed, started=in_progress, blocked=ask_user, failed=address error before later steps.\n"
        )

        settings_kwargs = self._build_reasoning_settings(reasoning_effort=reasoning_effort, reasoning_summary="auto")

        return Agent(
            name=planner_name,
            instructions=instructions,
            tools=[
                FunctionTool(
                    name="list_skills",
                    description="List available workflow skills/playbooks.",
                    params_json_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=list_skills_tool,
                ),
                FunctionTool(
                    name="read_skill_content",
                    description="Read a specific skill/playbook by skill_id.",
                    params_json_schema={
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "Skill ID to read."}
                        },
                        "required": ["skill_id"],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=read_skill_tool,
                ),
                FunctionTool(
                    name="list_agents_capabilities",
                    description="List discovered MCP agents and exact MCP tools.",
                    params_json_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=list_agents_capabilities_tool,
                ),
            ],
            handoffs=[],
            model=model,
            model_settings=ModelSettings(**settings_kwargs) if settings_kwargs else ModelSettings(),
            output_type=AgentOutputSchema(PlanReview, strict_json_schema=False),
        )

    async def build_planning_agent(self, *args, **kwargs) -> "Agent":
        """Backward-compatible alias for older callers."""
        if "planning_name" in kwargs and "planner_name" not in kwargs:
            kwargs["planner_name"] = kwargs.pop("planning_name")
        if "planning_instructions" in kwargs and "planner_instructions" not in kwargs:
            kwargs["planner_instructions"] = kwargs.pop("planning_instructions")
        return await self.build_planner_agent(*args, **kwargs)

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
            "1. For complex workflow planning, use the create_workflow_plan tool when it is available.\n"
            "2. For explicit single-step execution/control requests, hand off to the matching specialist agent.\n"
            "3. Never use handoff to carry a plan, user approval, or execution state.\n"
            "4. If a planning tool returns JSON, return that JSON to the caller without rewriting it."
        )

        settings_kwargs = self._build_reasoning_settings(reasoning_effort=reasoning_effort, reasoning_summary="auto")

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
        planner_name: str = "Planner",
        planner_model: Optional[Any] = None,
        planner_reasoning_effort: Optional[str] = "high",
        planner_instructions: Optional[str] = None,
        playbooks_dir: Optional[str] = None,
        orchestrator_name: Optional[str] = None,
        orchestrator_model: Optional[Any] = None,
        orchestrator_reasoning_effort: Optional[str] = None,
        enable_specialist_handoffs: bool = False,
        include_planner_tool_on_triage: bool = True,
    ) -> Tuple["Agent", Dict[str, "Agent"]]:
        """
        Build a multi-agent system where Triage uses Planner as a tool for plans.
        Handoffs are kept only for simple specialist routing.
        """
        if orchestrator_name:
            planner_name = orchestrator_name
        if orchestrator_model:
            planner_model = orchestrator_model
        if orchestrator_reasoning_effort:
            planner_reasoning_effort = orchestrator_reasoning_effort

        # 1. Build Specialist Agents from MCP
        sub_agents = await self.build_openai_sub_agents_from_fastmcp(
            exclude_agents=exclude_agents,
            include_resource_list_tool=include_resource_list_tool,
            tool_name_style=tool_name_style,
        )

        # 2. Build Planner as a callable tool, not a handoff target.
        planner = await self.build_planner_agent(
            sub_agents,
            model=planner_model or model,
            reasoning_effort=planner_reasoning_effort,
            planner_name=planner_name,
            planner_instructions=planner_instructions,
            playbooks_dir=playbooks_dir,
        )

        async def planner_output_extractor(run_result) -> str:
            output = run_result.final_output
            if isinstance(output, PlanReview):
                return output.model_dump_json()
            if hasattr(output, "model_dump_json"):
                return output.model_dump_json()
            return str(output)

        planner_tool = planner.as_tool(
            tool_name="create_workflow_plan",
            tool_description=(
                "Create a structured PlanReview for complex, multi-step, or workflow-style "
                "requests. This only plans and never executes MCP tools. The input should include "
                "the original user goal plus any constraints, current state, and success criteria."
            ),
            custom_output_extractor=planner_output_extractor,
        )

        # 3. Generate detailed summary for Triage.
        registry_summary = await self._generate_agents_registry_summary(sub_agents, detailed=True)

        # 4. Build Triage (The central hub).
        planning_rule = (
            "1. For complex, multi-step workflow requests, call create_workflow_plan and return its JSON result exactly.\n"
            "1a. If the user provides a new requirement, asks for a recommendation/suggestion, asks to change parameters, or asks to plan/re-plan, call create_workflow_plan. Do not answer such requests directly from conversation history.\n"
            "1b. If a pending plan is provided with a latest user message, call create_workflow_plan and let Planner decide whether to RESET to a fresh plan or REVISE the pending plan. Do not assume the new message continues the old plan.\n"
            "1c. If conversation history contains a blocked execution step or missing-input request, do not continue that step unless the latest user message explicitly answers it or says continue/resume/execute. A separate latest-message task must be planned fresh from step_id 1.\n"
            "1d. If UI action context or trigger output shows that a workflow tool already completed, pass that context into create_workflow_plan and let Planner start from the next incomplete skill step.\n"
        ) if include_planner_tool_on_triage else (
            "1. Planning is handled by the caller's direct Planner path. Do not create or revise plans here.\n"
            "1a. If the user asks for planning, recommendation, re-planning, or parameter-selection workflow design, tell the caller to use the dedicated Planner path instead of handling it in Triage.\n"
            "1b. Only handle direct immediate specialist execution/control requests here.\n"
        )

        combined_instructions = (
            f"{triage_instructions or ''}\n"
            "You are the Main Triage and Coordination Agent.\n"
            f"{planning_rule}"
            "2. For explicit immediate single-step execution/control requests, such as starting BF preview, getting SEM state, confirming ROI, or stopping preview, hand off to the matching specialist agent.\n"
            "3. Do not call specialist handoff tools to create a PlanReview. After the user approves a PlanReview, execute it by reading each step and handing off to that step's specialist agent.\n"
            "4. For approved PlanReview execution, follow the plan step order. Each handoff must include the step_id, exact tool_name, arguments, required_inputs, expected_output, on_success, and on_failure.\n"
            "5. If a direct specialist/tool is available, never say you lack control access. Route to the specialist instead.\n"
            "6. If the relevant planning or execution route fails, report the failure and stop.\n\n"
            f"{registry_summary}"
        )
        triage = await self.build_triage_agent(
            {}, # Specialists added via manual handoffs to control the registry
            model=model,
            reasoning_effort=triage_reasoning_effort,
            triage_name=triage_name,
            triage_instructions=combined_instructions,
        )
        if include_planner_tool_on_triage:
            triage.tools = list(getattr(triage, "tools", [])) + [planner_tool]

        # 5. Specialist handoffs are opt-in. Default workflow mode forbids fallback execution.
        if enable_specialist_handoffs:
            triage_handoffs = []
            for ag in sub_agents.values():
                triage_handoffs.append(handoff(
                    agent=ag,
                    tool_description_override=(
                        f"Hand off to {ag.name} for direct specialist execution/control requests, "
                        "single-step actions, or specialist questions. Do not use this handoff to pass a full PlanReview."
                    )
                ))
            triage.handoffs = triage_handoffs

            # Specialists -> Triage (Circular loop back)
            for ag in sub_agents.values():
                ag.handoffs = [handoff(
                    agent=triage,
                    tool_description_override="Return control to the Triage Agent after completing a task or if input is needed."
                )]
        else:
            triage.handoffs = []
            for ag in sub_agents.values():
                ag.handoffs = []

        all_agents = dict(sub_agents)
        all_agents[planner_name] = planner
        return triage, all_agents
