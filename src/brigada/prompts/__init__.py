"""Versioned system prompts. The hash goes into every run so results can be traced to the
exact prompt text that produced them."""

import hashlib
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    sha256: str


def load_prompt(name: str) -> Prompt:
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return Prompt(name, text, hashlib.sha256(text.encode()).hexdigest()[:12])
