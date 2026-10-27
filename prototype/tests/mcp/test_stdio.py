import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {
    "repair_mcp.repair_server": {"propose_patch", "escalate"},
    "repair_mcp.k8s_server": {"k8s_list", "k8s_get", "k8s_describe_pod", "k8s_logs", "k8s_events"},
    "repair_mcp.git_server": {"git_list", "git_read", "git_log", "git_diff"},
    "repair_mcp.argo_server": {"argo_app_for_namespace", "argo_app_status", "workflow_status"},
}


async def test_servers_list_tools_and_repair_writes(tmp_path):
    out = tmp_path / "out.json"
    import os

    env = {**os.environ, "REPAIR_AGENT_OUT": str(out)}
    for module, names in EXPECTED.items():
        params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            assert {t.name for t in (await s.list_tools()).tools} == names
            if module.endswith("repair_server"):
                res = await s.call_tool("escalate", {"reason": "because"})
                assert "stop now" in res.content[0].text
    assert json.loads(out.read_text())["status"] == "escalated"
