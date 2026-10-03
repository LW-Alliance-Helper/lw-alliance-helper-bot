"""Words, not codes, in the cells and headers of the tabs the bot writes (#729).

An officer reading a tab should see the words the bot shows in Discord
("Posted", "Opted out"), never the internal keys the code uses (`posted`,
`opt_out`). Each column that holds one of a fixed set of values gets a
`Words`: it writes the word, and reads back the code from the word, the old
code, or any other spelling the bot has used, in any case, so rows written
before this keep working. A column an officer edits by hand gets a dropdown
of the words (`sheet_format.TabSpec.dropdowns`).

Headers change the same way: `rename_headers` rewrites a header cell only
while it still holds the exact text the bot used to write there, so a header
an officer renamed stays theirs.

Changing a cell from a code to a word breaks a formula an alliance wrote
against the old code (`=COUNTIF(D:D, "posted")`), which is why the switch
gets a line in the patch notes.
"""

from __future__ import annotations


class Words:
    """The words for one column's fixed set of values.

    `words` maps each code to the word written in its place, in the order a
    dropdown lists them. `also` maps any other spelling the column may hold
    (an older label, a short form) to its code."""

    def __init__(self, words: dict[str, str], also: dict[str, str] | None = None):
        self.words = dict(words)
        self._codes: dict[str, str] = {}
        for code, word in self.words.items():
            self._codes[code.lower()] = code
            self._codes[word.strip().lower()] = code
        for spelling, code in (also or {}).items():
            self._codes.setdefault(spelling.strip().lower(), code)

    def word(self, code: str) -> str:
        """The word for `code`; a value with no word is written as it is."""
        return self.words.get(code, code)

    def code(self, cell: str) -> str:
        """The code a cell holds, from its word or any known spelling, any
        case. A cell holding something else reads as its lowercased text,
        which is how these columns were read before they had words."""
        text = (cell or "").strip()
        return self._codes.get(text.lower(), text.lower())

    def reword(self, cell: str) -> str:
        """A cell as the word for whatever it holds: an old code becomes its
        word, a blank stays blank, anything unknown stays as typed."""
        text = (cell or "").strip()
        if not text:
            return ""
        code = self._codes.get(text.lower())
        return self.words[code] if code in self.words else text

    @property
    def options(self) -> tuple[str, ...]:
        """The words, for a dropdown."""
        return tuple(self.words.values())


def rename_headers(ws, header: list[str], renames: dict[str, str], *, header_row: int = 1):
    """Rewrite each header cell that still holds an old bot header (the keys
    of `renames`, exact text) as its new one. `header` is updated in place.
    A cell an officer changed is left alone. Best-effort, one call."""
    updates = []
    for idx, text in enumerate(header):
        new = renames.get(text)
        if new and new != text:
            header[idx] = new
            updates.append({"range": f"{_col_letter(idx + 1)}{header_row}", "values": [[new]]})
    if not updates:
        return
    try:
        ws.batch_update(updates, value_input_option="RAW")
    except Exception as e:
        print(f"[SHEET WORDS] Could not rename headers on '{getattr(ws, 'title', '?')}': {e}")


def _col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out
