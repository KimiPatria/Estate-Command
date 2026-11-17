"""The chat is the app's front door (buildplan_chat_centre.md Part A).

/ redirects to /chat, the dashboard lives at /dashboard, and no page except the
dashboard itself links to it: the chat's sidebar carries Report and Forecast,
and every other page's nav is Chat / Report / Forecast.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent

# Every served page with a nav. The dashboard is the one page allowed to link to itself.
PAGES = {
    "chat": ROOT / "chat_static" / "chat.html",
    "report": ROOT / "report_static" / "report.html",
    "outlook": ROOT / "forecast_static" / "outlook.html",
    "forecast": ROOT / "forecast_static" / "forecast.html",
    "compare": ROOT / "forecast_static" / "compare.html",
    "model_health": ROOT / "forecast_static" / "model_health.html",
    "forecast_lab": ROOT / "experiment_static" / "forecast_lab.html",
    "evals": ROOT / "eval_static" / "evals.html",
    "command_src": ROOT / "command_static" / "index.html",
    "command_legacy": ROOT / "command_static" / "command.html",
    "command_dist": ROOT / "command_static" / "dist" / "index.html",
}
DASHBOARD = ROOT / "dashboard_static" / "index.html"


@pytest.fixture(scope="module")
def client():
    import dashboard_server
    return TestClient(dashboard_server.app)


def test_root_redirects_to_the_chat(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/chat"


def test_root_redirect_keeps_the_query_string(client):
    r = client.get("/?theme=dark&session=abc", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/chat?theme=dark&session=abc"


def test_dashboard_is_served_at_its_own_address(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'id="app-nav"' in r.text and "Dashboard Generator" in r.text
    assert r.headers.get("cache-control") == "no-cache"


def test_chat_is_still_at_chat(client):
    r = client.get("/chat")
    assert r.status_code == 200
    assert 'class="sb-pages"' in r.text


def _hrefs(html: str) -> set:
    return set(re.findall(r'href="([^"]+)"', html))


@pytest.mark.parametrize("name", sorted(PAGES))
def test_no_page_links_to_the_dashboard(name):
    hrefs = _hrefs(PAGES[name].read_text(encoding="utf-8"))
    assert "/" not in hrefs, f"{name} still links to /"
    assert "/dashboard" not in hrefs, f"{name} links to /dashboard"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_page_links_to_chat_report_and_forecast(name):
    hrefs = _hrefs(PAGES[name].read_text(encoding="utf-8"))
    assert {"/chat", "/report", "/forecast"} <= hrefs, f"{name}: {sorted(hrefs)}"


def test_dashboard_nav_points_at_dashboard():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert '<a href="/dashboard" class="nav-active">Dashboard</a>' in html
    assert 'href="/"' not in html


def test_chat_has_no_top_bar_pill_nav():
    html = PAGES["chat"].read_text(encoding="utf-8")
    assert 'class="app-nav"' not in html
    # Report and Forecast: in the sidebar, and as icons while it is closed.
    assert html.count('href="/report"') == 2 and html.count('href="/forecast"') == 2
