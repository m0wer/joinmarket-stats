import functools
import http.server
import threading
from datetime import UTC, datetime

import pytest
from conftest import model_history
from playwright.sync_api import sync_playwright

from jmstats.archive import write_json
from jmstats.market import normalize_market
from jmstats.render import render


@pytest.fixture
def dashboard(tmp_path, watcher_payload):
    root, site = tmp_path / "data", tmp_path / "site"
    model_history(root)
    write_json(
        root / "market.json",
        normalize_market(watcher_payload, datetime(2026, 10, 29, 10, tzinfo=UTC)),
    )
    write_json(
        root / "fee-estimates.json",
        {"months": {"2025-01": {"median_sats": 200, "median_ppm": 2000}}},
    )
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
        assert page.locator("#maker-fees-chart .bar").count() == 1
        assert "2025-01: 400 sats" in page.locator("#maker-fees-chart").text_content()
        assert "1 of 1 transactions solved" in page.locator("#maker-fees-chart").text_content()
        assert page.locator("#maker-fees-chart .fee-coverage-line").count() == 1
        assert (
            page.locator("#maker-fees-chart text").first.evaluate("e => e.getScreenCTM().a") > 0.8
        )
        assert page.locator("#activity-chart text").first.evaluate("e => e.getScreenCTM().a") > 0.8
        assert page.locator("#market-chart text").first.evaluate("e => e.getScreenCTM().a") > 0.8
        assert page.locator("#monthly-table tr").count() == 12
        assert "Median CJ size" in page.locator("#activity-cards").inner_text()
        assert "100,000" in page.locator("#activity-cards").inner_text()
        assert "200 sat / maker" in page.locator("#activity-cards").inner_text()
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
                    "conditional_gain_total_sats": total,
                    "solver_samples": samples,
                    "coverage": coverage,
                }
                for month, count, total, samples, coverage in [
                    ("2025-01", 3, 123_456_789, 2, "complete"),
                    ("2025-02", 2, 0, 2, "partial"),
                    ("2025-03", 1, None, 0, "complete"),
                    ("2025-04", None, None, 0, "unscanned"),
                    ("2025-05", 0, None, 0, "complete"),
                    ("2026-01", 1, 100, 1, "partial"),
                ]
            ]
            route.fulfill(json=payload)

        page.route("**/data.json", fee_data)
        page.goto(dashboard)
        page.wait_for_selector("#maker-fees-chart svg")
        chart = page.locator("#maker-fees-chart")
        assert chart.locator(".bar").count() == 1
        assert "2026-01: 100 sats" in chart.text_content()
        page.select_option("#year", "2025")
        assert chart.locator(".bar").count() == 2
        assert chart.locator(".bar.partial").count() == 1
        assert "2025-01: 123,456,789 sats" in chart.text_content()
        assert "2 of 3 transactions solved" in chart.text_content()
        assert "2025-02: 0 sats" in chart.text_content()
        assert "2025-03: fee estimates unavailable" in chart.text_content()
        assert "2025-04: unscanned" in chart.text_content()
        assert chart.locator(".fee-coverage-point").count() == 3
        assert "100%" in chart.text_content()
        assert "0%" in chart.text_content()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.select_option("#year", "all")
        assert chart.locator(".bar").count() == 3
        assert not errors
        browser.close()


def test_fee_chart_treats_older_json_without_total_as_unavailable(dashboard):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def older_data(route):
            payload = route.fetch().json()
            for month in payload["months"]:
                month.pop("conditional_gain_total_sats", None)
            route.fulfill(json=payload)

        page.route("**/data.json", older_data)
        page.goto(dashboard)
        page.wait_for_selector("#maker-fees-chart .empty")
        assert "No maker fee estimates" in page.locator("#maker-fees-chart").inner_text()
        assert not page.locator("#error").is_visible()
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
