import asyncio
import os

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from auth.token_manager import get_token_cached
from fabric.fabric_connection import get_fabric_connection, mcp_launch_command


async def run_agent(user_message: str, access_token: str):
    env = dict(os.environ)
    _c = get_fabric_connection()
    if _c.has_sp:
        env["AZURE_TENANT_ID"] = _c.tenant_id
        env["AZURE_CLIENT_ID"] = _c.client_id
        env["AZURE_CLIENT_SECRET"] = _c.client_secret
        env["AZURE_TOKEN_CREDENTIALS"] = "prod"
    elif access_token:
        env["PBI_MODELING_MCP_ACCESS_TOKEN"] = access_token
    command, args = mcp_launch_command()
    server_params = StdioServerParameters(
        command=command,
        args=args,
        env=env,
    )

    print("Starting MCP Server...")
    async with (
        stdio_client(server_params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        print("MCP Server ready.\n")

        tools_result = await session.list_tools()

        CORE_TOOLS = {
            "connection_operations",
            "table_operations",
            "measure_operations",
            "dax_query_operations",
        }

        tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            }
            for t in tools_result.tools
            if t.name in CORE_TOOLS
        ]
        print(
            f"{len(tools)} tools loaded ({len(tools_result.tools)} available on server).\n"
        )

        client = anthropic.Anthropic()

        _c = get_fabric_connection()
        system_prompt = f"""You are a Power BI semantic modeling assistant.
You have access to the Power BI Modeling MCP server tools.

At the start of every session, ALWAYS connect to the semantic model first:
- Workspace: '{_c.workspace}'
- Model: '{_c.models[0] if _c.models else ""}'

Use the connection_operations tool to connect, then proceed with the user request.
Always confirm what you are doing before making changes to the model.

IMPORTANT RULES:
- Only create, update, or delete measures/columns when the user explicitly says to add, create, update, or modify something in the model.
- If the user asks for data, numbers, or a breakdown, run a DAX query and return the results. Do NOT create measures for this.
- In DAX, a single measure automatically breaks down by any dimension on a visual — never create duplicate measures for different breakdowns."""

        system_blocks = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}

        messages = [{"role": "user", "content": user_message}]

        print(f"User: {user_message}\n")
        print("-" * 50)

        while True:
            for attempt in range(5):
                try:
                    response = client.messages.create(
                        model="claude-3-5-sonnet-latest",
                        max_tokens=4096,
                        system=system_blocks,
                        tools=tools,
                        messages=messages,
                        extra_headers={
                            "anthropic-beta": "prompt-caching-2024-07-25"
                        },
                    )
                    break
                except anthropic.RateLimitError:
                    wait = 2**attempt * 10
                    print(
                        f"\nRate limited. Waiting {wait}s before retry ({attempt + 1}/5)..."
                    )
                    await asyncio.sleep(wait)
                    if attempt == 4:
                        raise

            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if hasattr(block, "text") and block.text:
                    print(f"Agent: {block.text}")

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        print(f"\n  Tool call: {block.name}")
                        print(f"     Input: {block.input}")

                        result = await session.call_tool(block.name, block.input)
                        result_text = str(result.content)
                        MAX_RESULT_CHARS = 3000
                        truncated = result_text[:MAX_RESULT_CHARS]
                        if len(result_text) > MAX_RESULT_CHARS:
                            truncated += "\n...[truncated]"

                        print(
                            f"     Result: {result_text[:200]}{'...' if len(result_text) > 200 else ''}"
                        )

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": truncated,
                            }
                        )

                messages.append({"role": "user", "content": tool_results})

                if len(messages) > 7:
                    messages = messages[:1] + messages[-6:]

        print("\n" + "-" * 50)
        print("Done.")


def main():
    token = get_token_cached()

    print("\nWhat would you like to do with your Power BI model?")
    print("Examples:")
    print("  - List all measures in the Sales table")
    print("  - Add descriptions to all measures")
    print("  - Generate a French translation for the model")
    print("  - Rename all columns to use camelCase\n")

    user_input = input("Your request: ").strip()
    if not user_input:
        print("No input provided.")
        return

    asyncio.run(run_agent(user_input, token))


if __name__ == "__main__":
    main()
