"""An Agent entrypoint that answers with the bundled `translate` skill.

The skill ships inside the code bundle (`skills/translate/SKILL.md`). The Agent discovers it through
`SkillToolset`, loads it on demand and translates the text into Klingon-ish.
Deploy with `--entrypoint translate.py:run` and set `OPENAI_API_KEY` as a service secret.
"""

from functools import cache
from pathlib import Path

from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.skill_stores.file_system import FileSystemSkillStore
from haystack.tools import SkillToolset

SKILLS_DIR = Path(__file__).parent / "skills"


@cache
def _agent() -> Agent:
    # Built on the first call, not at import: the CLI imports this module to read the input schema,
    # and the generator needs OPENAI_API_KEY, which only the service has.
    agent = Agent(
        chat_generator=OpenAIChatGenerator(),
        tools=SkillToolset(FileSystemSkillStore(str(SKILLS_DIR))),
        system_prompt="You are a translator. Always load the matching skill before you answer.",
    )
    agent.warm_up()
    return agent


def run(text: str) -> dict:
    result = _agent().run(messages=[ChatMessage.from_user(f"Translate into Klingon-ish: {text}")])
    return {"translation": result["last_message"].text}
