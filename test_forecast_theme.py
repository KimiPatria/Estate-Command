"""The forecast pages share the chat page's look through a COPY of its tokens
(forecast_static/theme.css). These tests keep the copy honest and keep the three
pages on it."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
CHAT_CSS = ROOT / "chat_static" / "chat.css"
THEME_CSS = ROOT / "forecast_static" / "theme.css"
PAGES = {
    "forecast": ROOT / "forecast_static" / "forecast.html",
    "model_health": ROOT / "forecast_static" / "model_health.html",
    "forecast_lab": ROOT / "experiment_static" / "forecast_lab.html",
}


def _tokens(css: str, selector: str) -> dict:
    """Custom properties declared in the first `selector { ... }` block."""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, flags=re.S)
    assert m, f"no {selector} block"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: " ".join(v.split()) for k, v in re.findall(r"(--[\w-]+|color-scheme)\s*:\s*([^;]+);", body)}


@pytest.mark.parametrize("selector", [":root", ':root[data-theme="dark"]'])
def test_theme_tokens_equal_the_chat_tokens(selector):
    chat = _tokens(CHAT_CSS.read_text(encoding="utf-8"), selector)
    theme = _tokens(THEME_CSS.read_text(encoding="utf-8"), selector)
    assert chat, "chat tokens not found"
    assert theme == chat, (
        "forecast_static/theme.css has drifted from chat_static/chat.css: "
        + ", ".join(sorted(k for k in set(chat) | set(theme) if chat.get(k) != theme.get(k))))


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_uses_the_shared_theme(name):
    html = PAGES[name].read_text(encoding="utf-8")
    assert "/forecast-static/theme.css" in html
    assert "/forecast-static/theme.js" in html
    assert html.index("</style>") < html.index("/forecast-static/theme.css"), \
        "theme.css must load after the page's own <style> so the shared chrome wins"
    assert "localStorage.getItem('chat-theme')" in html, "theme must be set before first paint"
    assert 'id="theme-btn"' in html and 'class="topbar"' in html
    assert 'id="db-chip"' in html, "every forecast page carries the Database control"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_defines_no_tokens_or_cdn_scripts_of_its_own(name):
    html = PAGES[name].read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", html, flags=re.S).group(1)
    assert ":root" not in style, "page tokens belong in theme.css"
    assert not re.search(r"<script[^>]+src=\"https?://", html), "scripts are vendored, not loaded from a CDN"
    assert "nav-pill" not in html, "the old sliding nav is gone"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_css_has_no_hard_coded_surface_colours(name):
    """White, the old greys and the old gold must come from tokens, or dark mode breaks."""
    html = PAGES[name].read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", html, flags=re.S).group(1)
    for bad in ("#fff", "#ffffff", "#f7faf9", "#dce4e1", "#20312e", "#fdf3f2", "#9a7414"):
        assert bad not in style.lower(), f"{bad} is hard-coded in {name}'s CSS"
