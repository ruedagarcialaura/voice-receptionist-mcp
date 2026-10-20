"""Chat with the receptionist in the terminal.

The agent launches the MCP server as a subprocess and talks to it over stdio,
exactly as a desktop MCP host (e.g. Claude Desktop) would.

Run:  python -m agent.text_agent            (add --quiet to hide tool calls)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .core import ReceptionistAgent, llm_from_env

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GREETING = "Thank you for calling Lakeside Family Clinic. This is the virtual assistant. How can I help you today?"

DIM, CYAN, RED, RESET = "\033[2m", "\033[36m", "\033[31m", "\033[0m"


def print_event(kind: str, data: dict) -> None:
    if kind == "tool_call":
        print(f"{DIM}  -> {data['name']}({json.dumps(data['args'])}){RESET}")
    elif kind == "tool_result":
        try:
            result = json.loads(data["text"])
            status = "ok" if result.get("ok") else f"{RED}{result['error']['code']}{RESET}{DIM}"
        except (json.JSONDecodeError, KeyError, TypeError):
            status = "error" if data["is_error"] else "?"
        print(f"{DIM}  <- {status}{RESET}")
    elif kind == "llm_error":
        print(f"{RED}  LLM error: {data['error'][:200]}{RESET}")


async def main(quiet: bool) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "ehr_server.server"], env=dict(os.environ), cwd=str(PROJECT_ROOT)
    )
    llm, model = llm_from_env()

    async with Client(server) as mcp:
        agent = ReceptionistAgent(mcp, llm, model, on_event=None if quiet else print_event)
        await agent.start()
        print(f"{DIM}[model: {model} | {len(agent.tools)} MCP tools | type 'quit' to exit]{RESET}\n")
        print(f"{CYAN}Receptionist:{RESET} {GREETING}")
        agent.add_assistant_message(GREETING)

        while True:
            try:
                user = (await asyncio.to_thread(input, "\nYou: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user.lower() in {"quit", "exit"}:
                break
            if not user:
                continue
            reply, stats = await agent.respond(user)
            print(f"{CYAN}Receptionist:{RESET} {reply}")
            if not quiet:
                print(f"{DIM}  [{stats.total:.1f}s: LLM {sum(stats.llm_seconds):.1f}s x{len(stats.llm_seconds)}, "
                      f"tools {sum(stats.tool_seconds):.2f}s]{RESET}")


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
    asyncio.run(main(quiet="--quiet" in sys.argv))
