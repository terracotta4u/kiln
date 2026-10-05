import re
from importlib.resources import files

_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def read_prompt(name: str) -> str:
    return files("kiln.prompts").joinpath(name).read_text(encoding="utf-8")


def render_prompt(name: str, values: dict[str, str]) -> str:
    template = read_prompt(name)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(key)
        return values[key]

    return _PLACEHOLDER.sub(replace, template)
