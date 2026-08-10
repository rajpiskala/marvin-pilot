from __future__ import annotations

import hashlib
import re
from importlib.resources import files

ASSETS = files("marvin_pilot.visualizer_assets")


def _text(name: str) -> str:
    return ASSETS.joinpath(name).read_text(encoding="utf-8")


def _theme_tokens(css: str, theme: str) -> dict[str, str]:
    if theme == "light":
        pattern = r":root,\s*:root\[data-theme=\"light\"\]\s*\{(?P<body>.*?)\n\}"
    else:
        pattern = rf":root\[data-theme=\"{theme}\"\]\s*\{{(?P<body>.*?)\n\}}"
    match = re.search(pattern, css, re.DOTALL)
    assert match is not None
    return dict(re.findall(r"--([a-z-]+):\s*([^;]+);", match.group("body")))


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    brighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


def test_assets_are_offline_external_and_have_expected_controls() -> None:
    html = _text("index.html")
    assert '<script src="app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="styles.css">' in html
    assert 'src="marvin-pilot.png"' in html
    assert 'href="marvin-pilot.png"' in html
    assert "<textarea" not in html
    assert "style=" not in html
    assert 'data-theme-choice="light"' in html
    assert 'data-theme-choice="dusk"' in html
    assert 'data-theme-choice="night"' in html
    assert 'data-view-choice="split"' in html
    assert 'data-view-choice="before"' in html
    assert 'data-view-choice="after"' in html
    assert "Side by side" in html
    assert "Now only" in html
    assert "After only" in html
    assert "Review options" not in html


def test_vendored_pilot_art_matches_the_supplied_project_asset() -> None:
    artwork = ASSETS.joinpath("marvin-pilot.png").read_bytes()
    assert hashlib.sha256(artwork).hexdigest() == (
        "3fc59e9badcdcf5df23020205919c0d69fbf12bc5098baa2ef680dc70abd41a3"
    )


def test_javascript_uses_safe_dom_and_local_routes_only() -> None:
    script = _text("app.js")
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script
    assert "app.amazingmarvin.com" not in script
    assert "api/plan" in script
    assert "api/current" in script
    assert "marvinPilot.visualizer.theme.v1" in script
    assert "marvinPilot.visualizer.comparisonView.v1" in script
    assert script.count("window.localStorage.setItem") == 1
    assert "JSON.stringify(currentPlan)" not in script
    assert "paletteClass" in script
    assert 'article.setAttribute("aria-description", card.sparse_label)' in script


def test_sampled_theme_surfaces_and_shared_geometry_are_regression_locked() -> None:
    css = _text("styles.css")
    light = _theme_tokens(css, "light")
    dusk = _theme_tokens(css, "dusk")
    night = _theme_tokens(css, "night")
    assert (light["page-background"], light["content-background"], light["card-background"]) == (
        "#ffffff",
        "#fafbff",
        "#ffffff",
    )
    assert (dusk["page-background"], dusk["content-background"], dusk["card-background"]) == (
        "#252a48",
        "#252a48",
        "#2e3357",
    )
    assert (
        night["page-background"],
        night["content-background"],
        night["card-background"],
    ) == ("#16171b", "#1a1a1f", "#202125")
    assert "padding: 17px 17px 17px 12px" in css
    assert "border-radius: 9px" in css
    assert "font-size: 14px" in css
    assert "font-weight: 500" in css
    assert "line-height: 21px" in css
    assert "min-height: 55px" in css
    assert "border-left: 7px solid var(--update-edge)" in css
    assert "border-radius: 0 9px 9px 0" in css
    assert "border-left-color: var(--create-edge)" in css
    assert "border-left-color: var(--trash-edge)" in css
    assert ".task-item.palette-5" in css


def test_primary_and_small_status_text_meet_wcag_aa_contrast() -> None:
    css = _text("styles.css")
    themes = {name: _theme_tokens(css, name) for name in ("light", "dusk", "night")}
    for tokens in themes.values():
        assert _contrast(tokens["task-ink"], tokens["card-background"]) >= 4.5
        assert _contrast(tokens["muted-ink"], tokens["content-background"]) >= 4.5
        assert _contrast(tokens["faint-ink"], tokens["content-background"]) >= 4.5
    light = themes["light"]
    assert _contrast(light["create"], light["create-soft"]) >= 4.5
    assert _contrast(light["update"], light["update-soft"]) >= 4.5
    assert _contrast(light["trash"], light["trash-soft"]) >= 4.5
    for name in ("dusk", "night"):
        tokens = themes[name]
        assert _contrast(tokens["create"], tokens["content-background"]) >= 4.5
        assert _contrast(tokens["update"], tokens["content-background"]) >= 4.5
        assert _contrast(tokens["trash"], tokens["content-background"]) >= 4.5
