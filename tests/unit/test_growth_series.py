"""Tests for the growth time-series aggregation feeding GET /sheet/growth (#316).

Exercises ``build_growth_series`` (the pure core): per-period aggregation across
members, the derived active-member count, chronological ordering, numeric
parsing (commas / scientific notation / blanks), metrics added mid-history, and
the period→ISO date conversion.
"""

from __future__ import annotations

import growth


def test_build_empty_when_no_rows():
    assert growth.build_growth_series(["Power"], []) == {"metrics": ["Power"], "snapshots": []}


def test_aggregates_and_counts_active_members():
    rows = [
        ["Name", "Power (Jan 2026)", "Kills (Jan 2026)", "Power (Feb 2026)", "Kills (Feb 2026)"],
        ["Ada", "1000000", "50", "1200000", "60"],
        ["Bo", "2000000", "70", "", "80"],  # blank Power in Feb, still active via Kills
        ["Cy", "", "", "500000", "10"],  # only present from Feb
    ]
    out = growth.build_growth_series(["Power", "Kills"], rows)
    assert out["metrics"] == ["Power", "Kills"]
    snaps = {s["date"]: s for s in out["snapshots"]}

    jan = snaps["2026-01-01"]
    assert jan["values"] == {"Power": 3000000, "Kills": 120}
    assert jan["members"] == 2  # Ada, Bo (Cy blank both)

    feb = snaps["2026-02-01"]
    assert feb["values"] == {"Power": 1700000, "Kills": 150}  # Bo blank Power
    assert feb["members"] == 3  # Ada, Bo (via Kills), Cy


def test_periods_in_chronological_header_order():
    rows = [["Name", "Power (Jan 2026)", "Power (Feb 2026)"], ["Ada", "1", "2"]]
    out = growth.build_growth_series(["Power"], rows)
    assert [s["date"] for s in out["snapshots"]] == ["2026-01-01", "2026-02-01"]


def test_parses_commas_and_scientific_notation():
    rows = [["Name", "Power (Jan 2026)"], ["Ada", "1,000,000"], ["Bo", "2.5E+6"]]
    out = growth.build_growth_series(["Power"], rows)
    assert out["snapshots"][0]["values"]["Power"] == 3500000


def test_metric_added_later_absent_from_earlier_period():
    rows = [
        ["Name", "Power (Jan 2026)", "Power (Feb 2026)", "Kills (Feb 2026)"],
        ["Ada", "100", "200", "5"],
    ]
    out = growth.build_growth_series(["Power", "Kills"], rows)
    snaps = {s["date"]: s for s in out["snapshots"]}
    assert "Kills" not in snaps["2026-01-01"]["values"]
    assert snaps["2026-02-01"]["values"]["Kills"] == 5


def test_unparseable_period_label_falls_back_to_raw():
    out = growth.build_growth_series(["Power"], [["Name", "Power (Season 6)"], ["Ada", "10"]])
    assert out["snapshots"][0]["date"] == "Season 6"


def test_as_number_collapses_whole_floats_to_int():
    assert growth._as_number(100.0) == 100
    assert isinstance(growth._as_number(100.0), int)
    assert growth._as_number(100.5) == 100.5


def test_parse_cell_blank_is_none_zero_is_active():
    assert growth._parse_growth_cell("") is None
    assert growth._parse_growth_cell("   ") is None
    assert growth._parse_growth_cell("0") == 0.0  # recorded zero counts as active


def test_read_growth_series_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr("config.get_growth_config", lambda gid: {"metrics": [], "tab_growth": ""})
    assert growth.read_growth_series(123) == {"metrics": [], "snapshots": []}


# ── build_member_power_map (roster `power` source) ────────────────────────────


def test_build_member_power_map_latest_period_only():
    rows = [
        ["Name", "Power (Jan 2026)", "Kills (Jan 2026)", "Power (Feb 2026)", "Kills (Feb 2026)"],
        ["Ada", "1000000", "50", "1200000", "60"],
        ["Bo", "2000000", "70", "", ""],  # blank in latest period (Feb) → omitted
    ]
    out = growth.build_member_power_map(["Power", "Kills"], rows)
    assert out["ada"] == {"Power": 1200000, "Kills": 60}
    assert "bo" not in out


def test_build_member_power_map_empty_inputs():
    assert growth.build_member_power_map(["Power"], []) == {}
    assert growth.build_member_power_map([], [["Name", "Power (Jan 2026)"], ["Ada", "1"]]) == {}


def test_build_member_power_map_emits_configured_key_order():
    # Sheet header lists Kills before Power, but configured order is Power, Kills.
    # MM keys the roster's power columns off insertion order, so the emitted map
    # must follow the configured order, not the sheet layout.
    rows = [["Name", "Kills (Jan 2026)", "Power (Jan 2026)"], ["Ada", "60", "1200000"]]
    out = growth.build_member_power_map(["Power", "Kills"], rows)
    assert list(out["ada"].keys()) == ["Power", "Kills"]
