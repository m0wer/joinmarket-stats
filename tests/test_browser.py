import functools
import http.server
import threading
from datetime import UTC, datetime

import pytest
from conftest import model_history
from playwright.sync_api import sync_playwright

from jmstats.archive import write_json
from jmstats.fee_analysis import FeeStore
from jmstats.market import normalize_market
from jmstats.render import render
from jmstats.solver import Result


@pytest.fixture
def dashboard(tmp_path, watcher_payload):
    root, site = tmp_path / "data", tmp_path / "site"
    model_history(root)
    write_json(
        root / "market.json",
        normalize_market(watcher_payload, datetime(2026, 10, 29, 10, tzinfo=UTC)),
    )
    # Legacy imported medians must not be shown; the analysis is the evidence.
    write_json(
        root / "fee-estimates.json",
        {"months": {"2025-01": {"median_sats": 200, "median_ppm": 2000}}},
    )
    fees = FeeStore(root)
    fees.add_amounts(
        "ab" * 32,
        1736000000,
        [100_850, 149_750, 1_100_500],
        [100_000] * 3 + [1000, 50_000, 1_000_000],
    )
    fees.add_analysis("ab" * 32, Result(status="identified", low=400, high=400, gains=[150, 250]))
    fees.flush()
    write_json(
        root / "market-history.json",
        [
            {
                "as_of": "2026-10-29T09:00:00+00:00",
                "makers": 5000,
                "advertised_bonded_makers": 80,
                "reported_locked_sats": 30000000000,
                "offer_count": 5100,
            }
        ],
    )
    render(root, site, datetime(2026, 10, 29, 10, tzinfo=UTC))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("width", [1440, 375])
def test_dashboard_local_only_responsive_and_interactive(dashboard, width):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page.emulate_media(color_scheme="dark")
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(dashboard)
        page.wait_for_selector("#maker-cards .value")
        assert not page.locator("#error").is_visible()
        assert "CoinJoin" in page.title() or "JoinMarket" in page.title()
        assert all(url.startswith(dashboard) for url in requests)
        repository = page.locator("footer").get_by_role("link", name="GitHub", exact=True)
        assert repository.is_visible()
        assert repository.get_attribute("href") == "https://github.com/m0wer/joinmarket-stats"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator("form").count() == 0
        assert page.locator("#methodology").count() == 0
        assert "Privacy has a market" not in page.locator("body").inner_text()
        assert "unknown, conflicting" not in page.locator("body").inner_text()
        assert (
            page.evaluate("getComputedStyle(document.documentElement).backgroundColor")
            == "rgb(20, 26, 24)"
        )
        page.select_option("#theme", "light")
        assert (
            page.evaluate("getComputedStyle(document.documentElement).backgroundColor")
            == "rgb(246, 247, 244)"
        )
        page.reload()
        page.wait_for_selector("#maker-cards .value")
        assert page.locator("#theme").input_value() == "light"
        page.select_option("#theme", "system")
        assert (
            page.evaluate("getComputedStyle(document.documentElement).backgroundColor")
            == "rgb(20, 26, 24)"
        )
        page.select_option("#year", "2025")
        assert page.locator("#activity-chart .volume-line").count() == 1
        assert page.locator("#activity-chart .bar").count() > 0
        assert page.locator("#maker-fees-chart .fee-month").count() == 1
        fee_text = page.locator("#maker-fees-chart").text_content()
        assert "2025-01: 400 sats estimated, 400 exact" in fee_text
        assert "1 identified, 0 bounded, 0 unresolved, 0 unanalyzed" in fee_text
        assert page.locator("#maker-fees-chart .fee-range").count() == 0
        assert (
            page.locator("#maker-fees-chart text").first.evaluate("e => e.getScreenCTM().a") > 0.8
        )
        assert page.locator("#activity-chart text").first.evaluate("e => e.getScreenCTM().a") > 0.8
        assert page.locator("#market-chart text").first.evaluate("e => e.getScreenCTM().a") > 0.8
        assert page.locator("#monthly-table tr").count() == 12
        assert "Median CJ size" in page.locator("#activity-cards").inner_text()
        assert "100,000" in page.locator("#activity-cards").inner_text()
        cards = page.locator("#activity-cards").inner_text()
        assert "150 sat / maker" in cards
        assert "2 exactly known makers; p90 250 sat" in cards
        assert page.locator("#market-chart .maker-line").count() == 1
        assert page.locator("#market-chart .bonded-line").count() == 1
        assert page.locator("#market-chart .capital-line").count() == 1
        assert "5,000" in page.locator("#market-chart").inner_text()
        assert "Annual ROI on bond" in page.locator("#maker-result").inner_text()
        assert "nonnegative maker gains" in page.locator("#maker-assumptions").inner_text()
        assert "90-day" in page.locator("#maker-period").inner_text()
        assert "365 days" in page.locator("#maker-comparison").inner_text()
        assert "inferred fee eligibility" in page.locator("#maker-drivers").inner_text()
        assert "uncontested" not in page.locator("#maker-result").inner_text()
        assert "Optimal relative fee" in page.locator("#maker-cards").inner_text()
        assert "(optimal)" in page.locator("#maker-curve").inner_text()
        assert "bond^1.3" in page.locator("#maker-bond-note").inner_text()
        assert "ppm" not in page.locator("body").inner_text()
        assert "%" in page.locator("#maker-cards .card").first.inner_text()
        assert page.locator("#year option").evaluate_all("els => els.map(e => e.value)") == [
            "all",
            "2026",
            "2025",
            "2024",  # The synthetic archive has a scanned, zero-CJ December block.
        ]
        page.select_option("#year", "all")
        assert page.locator("#monthly-table tr").count() == 23
        assert "2020" not in page.locator("#activity-chart").text_content()
        assert "2024-12: 0 transactions" in page.locator("#activity-chart").text_content()
        assert not errors
        browser.close()


@pytest.mark.parametrize("width", [1440, 375])
def test_fee_chart_sats_zeroes_gaps_coverage_and_year_filter(dashboard, width):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def fee_data(route):
            payload = route.fetch().json()
            template = payload["months"][0]
            payload["months"] = [
                {
                    **template,
                    "month": month,
                    "count": count,
                    "volume_sats": 0,
                    "maker_fee_estimate_sats": estimate,
                    "maker_fee_exact_sats": exact,
                    "maker_fee_low_sats": low,
                    "maker_fee_high_sats": high,
                    "maker_fee_partial": partial,
                    "maker_fee_identified": 2,
                    "inconsistent": 1,
                    "coverage": coverage,
                }
                for month, count, estimate, exact, low, high, partial, coverage in [
                    ("2025-01", 3, 123_456_789, 10**8, 12 * 10**7, 13 * 10**7, False, "complete"),
                    ("2025-02", 2, 0, 0, 0, 0, False, "partial"),
                    ("2025-03", 1, None, 0, None, None, False, "complete"),
                    ("2025-04", None, None, None, None, None, False, "unscanned"),
                    ("2025-05", 0, None, None, None, None, False, "complete"),
                    ("2025-06", 4, 500, 200, 300, 900, True, "complete"),
                    ("2026-01", 1, 100, 100, 100, 100, False, "partial"),
                ]
            ]
            route.fulfill(json=payload)

        page.route("**/data.json", fee_data)
        page.goto(dashboard)
        page.wait_for_selector("#maker-fees-chart svg")
        chart = page.locator("#maker-fees-chart")
        assert chart.locator(".fee-month").count() == 1
        assert "2026-01: 100 sats estimated" in chart.text_content()
        page.select_option("#year", "2025")
        assert chart.locator(".fee-month").count() == 3
        assert chart.locator(".fee-exact").count() == 3
        assert chart.locator(".fee-partial").count() == 2
        assert chart.locator(".fee-range").count() == 2
        text = chart.text_content()
        assert "2025-01: 123,456,789 sats estimated, 100,000,000 exact" in text
        assert "range 120,000,000 to 130,000,000" in text
        assert "1 inconsistent excluded" in text
        assert "2025-02: 0 sats estimated" in text
        assert "2025-03: no maker fee estimate" in text
        assert "2025-04: unscanned" in text
        assert "Solved" not in text and "%" not in text
        activity = page.locator("#activity-chart").text_content()
        assert "2025-01: 3 transactions, 0 BTC, complete; 1 inconsistent with" in activity
        assert "5 inconsistent excluded" in page.locator("#activity-cards").inner_text()
        # The estimate bar sits on top of its exact part, within the axis.
        exact = chart.locator(".fee-exact").first.bounding_box()
        imputed = chart.locator(".fee-imputed").first.bounding_box()
        assert imputed["y"] + imputed["height"] == pytest.approx(exact["y"], abs=0.5)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.select_option("#year", "all")
        assert chart.locator(".fee-month").count() == 4
        assert not errors
        browser.close()


def test_fee_chart_without_estimates_and_older_json_versions(dashboard):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        version = {"schema_version": 2}

        def data(route):
            payload = route.fetch().json()
            payload.update(version)
            for month in payload["months"]:
                month.pop("maker_fee_estimate_sats", None)
            route.fulfill(json=payload)

        page.route("**/data.json", data)
        page.goto(dashboard)
        page.wait_for_selector("#maker-fees-chart .empty")
        assert "No maker fee estimates" in page.locator("#maker-fees-chart").inner_text()
        assert not page.locator("#error").is_visible()
        version["schema_version"] = 1
        page.reload()
        page.wait_for_selector("#error", state="visible")
        assert "Unsupported data version" in page.locator("#error").inner_text()
        browser.close()


@pytest.mark.parametrize("has_observations", [True, False])
def test_activity_history_keeps_zeroes_and_gaps_but_not_empty_leading_years(
    dashboard, has_observations
):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def activity_history(route):
            payload = route.fetch().json()
            for month in payload["months"]:
                month.update(count=None, volume_sats=None, coverage="unscanned")
                if has_observations and month["month"] in ("2025-03", "2025-05"):
                    month.update(count=0, volume_sats=0, coverage="complete")
            route.fulfill(json=payload)

        page.route("**/data.json", activity_history)
        page.goto(dashboard)
        page.wait_for_selector("#maker-cards .value")
        assert not page.locator("#error").is_visible()
        years = page.locator("#year option").evaluate_all("els => els.map(e => e.value)")
        page.select_option("#year", "all")
        if has_observations:
            assert years == ["all", "2025"]
            assert page.locator("#monthly-table tr").count() == 20
            first_month = page.locator("#monthly-table tr").last.locator("td").first
            assert first_month.text_content() == "2025-03"
            chart = page.locator("#activity-chart")
            assert "2025-04: unscanned" in chart.text_content()
            assert "2025-03: 0 transactions" in chart.text_content()
            assert chart.locator("rect.bar").count() == 2
        else:
            assert years == ["all"]
            assert page.locator("#monthly-table tr").count() == 0
            assert "No scanned activity yet" in page.locator("#activity-chart").inner_text()
        browser.close()
