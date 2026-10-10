from __future__ import annotations

import pytest

from resilience_map.frontend import charts, views
from tests.unit.test_frontend import POLY

# 120 synthetic tracts: heat falls with canopy and with income (the pattern the charts should reveal)


def make(n=120):
    feats = []
    for i in range(n):
        canopy = (i % 40) * 1.2  # 0..46.8
        income = 25000 + (i % 30) * 4000
        heat = 4.0 - 0.15 * canopy - 0.00004 * (income - 60000)
        score = 20 + 80 * ((i % 40) / 39)
        feats.append({"type": "Feature", "id": str(i), "geometry": POLY, "properties": {
            "tract_id": f"{i:011d}", "name": f"T{i}", "county_id": "48453", "population": 1000 + i,
            "score": None if i % 17 == 0 else score,
            "indicators": {"heat": heat, "canopy": canopy, "income": income, "age65": 10.0, "transit": None}}})  # fmt: skip
    return feats


COUNTIES = [{"county_id": f"0600{i}", "name": f"C{i} County", "tracts": 50, "population": 100000 * (i + 1),
             "high_tracts": 5 * (i + 1), "high_population": 9000 * (i + 1)} for i in range(10)]  # fmt: skip


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_all_charts_build_in_both_themes(theme):
    specs = charts.build_all(
        {"tracts": {"features": make()}, "counties": COUNTIES, "county": "06003"}, theme
    )
    assert [s.key for s in specs] == ["bands", "equity", "rel", "county"]
    for s in specs:
        assert s.fig is not None and s.rows and s.columns and s.title
        assert all(len(r) == len(s.columns) for r in s.rows)  # table twin matches its header
        assert views.chart_card(s) is not None


def test_titles_state_the_finding_not_just_the_topic():
    specs = {
        s.key: s
        for s in charts.build_all(
            {"tracts": {"features": make()}, "counties": COUNTIES, "county": None}, "light"
        )
    }
    assert "cooler surfaces" in specs["rel"].title and "r = -" in specs["rel"].title
    assert (
        "hotter than the highest-income" in specs["equity"].title
        or "cooler" in specs["equity"].title
    )
    assert "residents live in higher-vulnerability" in specs["bands"].title
    assert specs["county"].title.startswith("C9 County")  # largest high-vulnerability population


def test_ordinal_band_colors_are_monotone_and_flip_in_dark():
    light, dark = charts.PAL["light"]["bands"], charts.PAL["dark"]["bands"]
    assert (light["Lower vulnerability"], light["Higher vulnerability"]) == ("#86b6ef", "#184f95")
    assert (dark["Lower vulnerability"], dark["Higher vulnerability"]) == (
        "#1c5cab",
        "#86b6ef",
    )  # validated on #262626


def test_emphasis_highlights_only_the_selected_county():
    spec = charts.county_chart(COUNTIES, "06003", "light")
    colors = list(spec.fig.data[0].marker.color)
    assert colors.count(charts.PAL["light"]["series1"]) == 1 and len(set(colors)) == 2


def test_sparse_or_single_county_views_degrade_gracefully():
    assert charts.relationship_chart(make(10), "light").fig is None
    assert charts.equity_chart(make(10), "light").empty
    assert charts.county_chart(COUNTIES[:1], None, "light").fig is None
    assert charts.band_chart([], "light").fig is None
    assert charts.build_all(None, "light") == [] and views.insights_panel([]) is not None


def test_coverage_bar_and_meters_render_and_account_for_everyone():
    cov = {"total_population": 1000, "baseline_served": 400, "newly_served": 250}
    html = str(views.coverage_bar(cov).to_plotly_json())
    assert "Already served" in html and "Newly served" in html and "Not served" in html
    assert "35.0%" in html  # 1000 - 400 - 250 = 350 not served
    sites = [{"name": "Gym", "site_id": "S1", "population_assigned": 3000, "capacity": 3500}]
    assert "86%" in str(views.site_meters(sites).to_plotly_json())
    assert views.site_meters([]) is not None


def test_bars_have_no_outline_ink():
    specs = charts.build_all(
        {"tracts": {"features": make()}, "counties": COUNTIES, "county": "06003"}, "dark"
    )
    for s in specs:
        for trace in s.fig.data:
            if trace.type == "bar":
                assert (
                    trace.marker.line.width == 0
                )  # separation comes from gaps, never from strokes
