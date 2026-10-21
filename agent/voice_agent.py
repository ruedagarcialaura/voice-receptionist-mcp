"""Voice receptionist built with LiveKit Agents.

Pipeline: microphone -> VAD (Silero) -> speech-to-text (Groq Whisper) -> LLM (Groq) -> text-to-speech
(Groq Orpheus while developing, ElevenLabs for the demo). The LLM uses the EHR's MCP tools over HTTP,
so every booking rule is still enforced by the server.

Start the EHR service first:  python -m ehr_server.server --http
Then:  python -m agent.voice_agent console   talk from the terminal (laptop mic and speakers)
       python -m agent.voice_agent dev       connect to LiveKit Cloud and talk in the Agents Playground
"""

import os

from dotenv import load_dotenv
from livekit.agents import NOT_GIVEN, Agent, AgentServer, AgentSession, JobContext, JobProcess, cli
from livekit.agents.llm.mcp import MCPServerHTTP
from livekit.plugins import elevenlabs, groq, silero
from mcp import Client

from .core import build_instructions

load_dotenv()
MCP_URL = os.getenv("EHR_MCP_URL", "http://localhost:8000/mcp")
GREETING = "Thank you for calling Lakeside Family Clinic. How can I help you today?"

# Laptop-friendly: keep one warm process (not several) and accept calls until the CPU is really full.
server = AgentServer(num_idle_processes=1, load_threshold=1.0)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()  # load the voice-activity model once, not on every call


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    key = os.environ["LLM_API_KEY"]
    async with Client(MCP_URL) as ehr:
        instructions = await build_instructions(ehr)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=groq.STT(api_key=key),
        llm=groq.LLM(model=os.environ["LLM_MODEL"], api_key=key,
                     reasoning_effort=os.getenv("LLM_REASONING_EFFORT") or NOT_GIVEN),
        tts=elevenlabs.TTS() if os.getenv("TTS_PROVIDER") == "elevenlabs" else groq.TTS(api_key=key),
        mcp_servers=[MCPServerHTTP(MCP_URL)],
        max_tool_steps=8,
        min_endpointing_delay=0.8,  # wait a bit longer before deciding the caller finished (avoids cutting them off)
        preemptive_generation=False,  # only answer once the caller's turn is complete
    )
    await session.start(agent=Agent(instructions=instructions), room=ctx.room)
    await session.say(GREETING)  # fixed greeting: instant and always correct


if __name__ == "__main__":
    cli.run_app(server)
