"""mcp-1wrap: 1Password launcher for MCP servers.

Reads secrets through the 1Password CLI (``op``), prepares configuration
(for example OpenStack ``clouds.yaml`` / ``secure.yaml`` in a private
runtime directory) and then replaces itself with the real MCP server
command, so the client talks to the server directly over stdio.
"""

__version__ = "0.2.0"
