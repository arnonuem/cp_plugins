"""A minimal REAL stdio MCP server, spawned as a subprocess by AC-50.

AC-50 asks whether the MCP singleton carries N parallel runs. Mocking the MCP
layer would answer a different question, so the test drives a genuine server
over a genuine stdio transport.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cp-discord-ac50")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back, prefixed, so a run can be traced to its channel."""
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run()
