"""
Externalized prompt templates (Bedrock migration seam).

Every inline prompt string in the pipeline lives here as a .txt file, separate
from control-flow code, so prompt phrasing can be tuned per model family
(Llama/GPT-OSS vs. Nova) without touching pipeline code.

Resolution order for load_prompt("name"):
  1. prompts/name.<LLM_PROVIDER>.txt   (e.g. sql_system_preamble.bedrock.txt)
  2. prompts/name.txt                  (the default, tuned for today's Groq models)

Substitution uses literal {{KEY}} tokens (not str.format) so templates and
substituted values may contain JSON braces freely.
"""

from pathlib import Path
from threading import Lock

from config import LLM_PROVIDER

_DIR = Path(__file__).parent
_cache: dict[str, str] = {}
_lock = Lock()


def load_prompt(name: str, **subs) -> str:
    """Load a prompt template by name and substitute {{KEY}} placeholders.

    Keyword names are case-insensitive: table_list=... fills {{TABLE_LIST}}.
    """
    text = _read(name)
    for key, value in subs.items():
        text = text.replace("{{" + key.upper() + "}}", str(value))
    return text


def reload_prompts() -> None:
    """Drop the template cache (templates re-read from disk on next use)."""
    with _lock:
        _cache.clear()


def _read(name: str) -> str:
    with _lock:
        if name in _cache:
            return _cache[name]
    for candidate in (f"{name}.{LLM_PROVIDER}.txt", f"{name}.txt"):
        path = _DIR / candidate
        if path.exists():
            # rstrip only the trailing newline editors append; leading/inline
            # whitespace is part of the prompt.
            text = path.read_text(encoding="utf-8").rstrip("\n")
            with _lock:
                _cache[name] = text
            return text
    raise FileNotFoundError(f"No prompt template '{name}' in {_DIR}")
