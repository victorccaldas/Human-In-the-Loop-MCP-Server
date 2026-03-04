# Bootstrap wrapper for hitl-mcp-server.
# Ensures the tool is installed via uv before running it.
# MCP uses stdio transport — hitl-mcp-server inherits stdin/stdout.

$installed = uv tool list 2>$null | Select-String -SimpleMatch 'hitl-mcp-server' -Quiet
if (-not $installed) {
    uv tool install "hitl-mcp-server @ git+https://github.com/victorccaldas/Human-In-the-Loop-MCP-Server.git" --force 2>$null
}

# Run the server (inherits stdio for MCP transport)
hitl-mcp-server
