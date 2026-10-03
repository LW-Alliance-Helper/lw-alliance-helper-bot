"""Words, not codes, in bot-written tabs (#729)."""

from sheet_words import Words, rename_headers

W = Words({"opt_out": "Opted out", "skip_until": "Skipped until"}, also={"opt out": "opt_out"})


def test_word_and_code_both_ways():
    assert W.word("opt_out") == "Opted out"
    assert W.code("Opted out") == "opt_out"
    assert W.code("  OPTED OUT ") == "opt_out"
    assert W.code("opt_out") == "opt_out"
    assert W.code("Opt Out") == "opt_out"


def test_unknown_values_pass_through():
    assert W.word("something") == "something"
    assert W.code("Something Else") == "something else"
    assert W.reword("Something Else") == "Something Else"
    assert W.reword("") == ""
    assert W.reword("skip_until") == "Skipped until"


def test_options_are_the_words_in_order():
    assert W.options == ("Opted out", "Skipped until")


class _WS:
    def __init__(self):
        self.calls = []

    def batch_update(self, data, value_input_option=None):
        self.calls.append(data)


def test_rename_headers_only_touches_the_old_text():
    ws = _WS()
    header = ["Member", "Value", "My notes"]
    rename_headers(ws, header, {"Value": "Skip Until", "Notes": "Notes"})
    assert header == ["Member", "Skip Until", "My notes"]
    assert ws.calls == [[{"range": "B1", "values": [["Skip Until"]]}]]


def test_rename_headers_does_nothing_when_nothing_matches():
    ws = _WS()
    rename_headers(ws, ["Member", "Until"], {"Value": "Skip Until"})
    assert ws.calls == []
