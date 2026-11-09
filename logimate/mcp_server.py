"""Official MCP SDK stdio server. stdout is exclusively protocol traffic."""
import asyncio
import json
import os
import sys

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from .tool_gateway import Principal, ReadArguments, ToolGateway, TOOLS


def create_server(gateway: ToolGateway, policy_gateway=None):
    async def list_tools(context, params):
        tools = [types.Tool(
            name=name, description=f"Read synthetic {domain} record; deployment tenant only; read-v1.",
            inputSchema=ReadArguments.model_json_schema(),
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False))
            for name, domain in TOOLS.items() if domain in gateway.principal.domains]
        if policy_gateway is not None:
            from .policy_tools import NAME, PolicyArguments
            tools.append(types.Tool(name=NAME, description='Read synthetic policy context only; never order facts or payment evidence.',
                inputSchema=PolicyArguments.model_json_schema(), annotations=types.ToolAnnotations(
                    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)))
        return types.ListToolsResult(tools=tools)

    async def call_tool(context, params):
        from .policy_tools import NAME
        data = (await policy_gateway.execute(params.arguments or {}) if policy_gateway is not None and params.name == NAME
                else await gateway.execute(params.name, params.arguments or {}))
        if data["error"] == "UNKNOWN_TOOL":
            raise MCPError(-32602, "Unknown tool", data=data)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))],
                                    structuredContent=data, isError=not data["ok"])

    server = Server("logimate-synthetic-tools", version=getattr(policy_gateway, 'server_version', '0.5.0') if policy_gateway is not None else "0.4.0",
                    on_list_tools=list_tools, on_call_tool=call_tool)
    server.middleware = []  # No automatic telemetry export of business payloads.
    return server


async def main():
    # No fallback identity: trusted parent process explicitly provisions this subprocess.
    tenant = os.environ.get("LOGIMATE_MCP_TENANT", "")
    domains = frozenset(filter(None, os.environ.get("LOGIMATE_MCP_DOMAINS", "").split(",")))
    principal = Principal(tenant, domains)
    policy_gateway = None
    if os.environ.get('LOGIMATE_MCP_KNOWLEDGE') == '1':
        from .policy_tools import PolicyGateway
        policy_gateway = PolicyGateway(principal)
        if 'LOGIMATE_MCP_SHARED_URL' in os.environ:
            from .shared_policy import SharedPolicyGateway
            policy_gateway = SharedPolicyGateway(principal, {'url': os.environ['LOGIMATE_MCP_SHARED_URL'],
                'token': os.environ.get('LOGIMATE_MCP_SHARED_TOKEN', '')})
    server = create_server(ToolGateway(principal), policy_gateway)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError:
        print("Invalid or missing synthetic MCP deployment identity.", file=sys.stderr)
        raise SystemExit(2)
