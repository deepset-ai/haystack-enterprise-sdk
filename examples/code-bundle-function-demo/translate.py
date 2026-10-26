"""An Agent that answers with the bundled `translate` skill.

The skill ships inside the code bundle (`skills/translate/SKILL.md`). The Agent discovers it through
`SkillToolset`, loads it on demand and translates the text into Klingon-ish. Two entrypoints:

- `translate.py:agent`: an agent build. Chat, chat-stream and OpenAI chat completions work.
- `translate.py:run`: a function build. Call `/run` with `{"text": "..."}`.

Set `OPENAI_API_KEY` as a service secret. It is read on the first call, not at import.
"""

from pathlib import Path

from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.skill_stores.file_system import FileSystemSkillStore
from haystack.tools import SkillToolset

SKILLS_DIR = Path(__file__).parent / "skills"

agent = Agent(
    chat_generator=OpenAIChatGenerator(),
    tools=SkillToolset(FileSystemSkillStore(SKILLS_DIR)),
    system_prompt="You are a translator into Klingon-ish. Always load the translate skill before you answer.",
)


def run(text: str) -> dict:
    result = agent.run(messages=[ChatMessage.from_user(text)])
    return {"translation": result["last_message"].text}
