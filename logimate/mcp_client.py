"""Local trusted launcher; SDK performs initialize + notifications/initialized."""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .tool_gateway import Principal

ROOT = Path(__file__).resolve().parent.parent


def server_parameters(principal: Principal, *, knowledge=False, shared_retrieval=None):
    # Allowlisted OS essentials only; no model keys, tracing config or proxy credentials.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC",
               "HOME", "USERPROFILE"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(LOGIMATE_MCP_TENANT=principal.tenant_id,
               LOGIMATE_MCP_DOMAINS=",".join(sorted(principal.domains)),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    if knowledge:
        env['LOGIMATE_MCP_KNOWLEDGE'] = '1'
        for name in ('LOGIMATE_DENSE_BACKEND', 'LOGIMATE_MILVUS_URI', 'LOGIMATE_MILVUS_TOKEN',
                     'LOGIMATE_RETRIEVAL_CACHE', 'LOGIMATE_LEXICAL_CACHE'):
            if name in os.environ: env[name] = os.environ[name]
    if shared_retrieval is not None:
        from .shared_policy import validate_connection
        validate_connection(shared_retrieval)
        if not knowledge: raise ValueError('shared retrieval requires knowledge capability')
        for name in ('LOGIMATE_MILVUS_URI', 'LOGIMATE_MILVUS_TOKEN', 'LOGIMATE_RETRIEVAL_CACHE', 'LOGIMATE_LEXICAL_CACHE'):
            env.pop(name, None)
        env['LOGIMATE_MCP_SHARED_URL'] = shared_retrieval['url']
        env['LOGIMATE_MCP_SHARED_TOKEN'] = shared_retrieval['token']
    return StdioServerParameters(command=sys.executable, args=["-m", "logimate.mcp_server"],
                                 cwd=ROOT, env=env)


@asynccontextmanager
async def connect(principal: Principal, *, knowledge=False, shared_retrieval=None):
    async with stdio_client(server_parameters(principal, knowledge=knowledge, shared_retrieval=shared_retrieval)) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=5) as session:
            initialized = await session.initialize()
            if initialized.server_info.name != "logimate-synthetic-tools" or initialized.capabilities.tools is None:
                raise RuntimeError("unexpected MCP server identity or capability")
            yield session, initialized
