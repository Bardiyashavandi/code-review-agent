"""
adk_demo.py
-----------
Small standalone script for the video demo: shows the ADK agent deciding,
on its own, to call review_repo_tool from a plain-language request.

Run it directly:
    python3 adk_demo.py
"""

import asyncio
import os

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent import build_adk_agent

PROMPT = (
    "review https://github.com/anxolerd/dvpwa (branch: master) "
    "and summarize the top issues"
)


async def main() -> None:
    load_dotenv()  # loads .env into os.environ if present; does not override existing env vars

    adk_agent = build_adk_agent(
        github_token=os.environ["GITHUB_TOKEN"],
        gemini_api_key=os.environ["GEMINI_API_KEY"],
    )

    runner = InMemoryRunner(agent=adk_agent, app_name="code_review_agent")
    session = await runner.session_service.create_session(
        app_name="code_review_agent", user_id="demo_user"
    )

    message = types.Content(role="user", parts=[types.Part(text=PROMPT)])

    print(f"Prompt: {PROMPT}\n")
    async for event in runner.run_async(
        user_id="demo_user", session_id=session.id, new_message=message
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "function_call", None):
                    print(f"[agent decided to call tool: {part.function_call.name}]")
                if getattr(part, "text", None):
                    print(part.text)


if __name__ == "__main__":
    asyncio.run(main())
