"""Fill `{placeholder}`s in text an alliance wrote, without ever crashing.

Leadership types these templates themselves, so a typo like `{nme}` is a
normal input rather than an error: it renders literally and the post still
goes out. A `str.format` that raised would instead take the whole loop tick
down with it, for every guild after this one.

The same idiom is pasted privately into `train_cog`, `storm_log`, `buddy_ui`
and `shiny_tasks`. This is its shared home from Leadership Duties (#687) on;
those four move over as they are next touched.
"""

from __future__ import annotations


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, **values: str) -> str:
    """`template` with each known `{key}` replaced and anything else left as
    typed, including unknown keys, stray braces and format specs."""
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError, AttributeError, TypeError):
        # A lone `{`, a positional `{0}` or an attribute lookup like
        # `{name.x}`: fall back to plain substitution of the known keys.
        out = template
        for key, value in values.items():
            out = out.replace("{" + key + "}", value)
        return out
