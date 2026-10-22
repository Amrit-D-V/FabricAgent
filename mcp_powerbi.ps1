# Launcher for the Power BI Modeling MCP server (used by .mcp.json).
# Loads the AZURE_* service-principal creds from the repo .env at runtime so
# Claude Code does NOT need them pre-exported in the shell. Secrets stay in
# .env only — this file and .mcp.json are safe to commit.
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*(AZURE_[A-Z_]+)\s*=\s*(.+?)\s*$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim('"'))
        }
    }
}
# Restrict Azure Identity to non-interactive (service principal) credentials —
# without this the MCP's DefaultAzureCredential chain can fall through to an
# interactive "Pick an account" browser popup on every spawn.
$env:AZURE_TOKEN_CREDENTIALS = "prod"
# --authmode=serviceprincipal makes the server use the AZURE_* creds above
# instead of an interactive browser sign-in. NOTE: the flag only exists from
# server 0.5.0 onward -- 0.4.0 parsed it as an unknown arg, silently ignored it,
# and fell back to a cached interactive user token (which produced a misleading
# "<user> does not have permission to call the Discover method" error).
# Even on 0.5.0 the server's own SP token is rejected by ADOMD cluster
# resolution (401), so FabricBI reads the live model through fabric/pbi_xmla.py
# (pbi_model_inspect / pbi_dax_query) rather than the MCP modeling tools.
& "C:\MCPServers\PowerBIModelingMCP\extension\server\powerbi-modeling-mcp.exe" --start --skip-confirmation --authmode=serviceprincipal
exit $LASTEXITCODE
