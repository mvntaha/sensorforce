"""Tests for the shared stylesheet and the Jinja templates.

These guard the rules that are easy to break silently as pages are added:
one stylesheet, no inline styles, every required token defined, the safety
disclaimer present, and status shown as text rather than colour alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = REPO_ROOT / "web" / "static" / "css" / "freshguard.css"
TEMPLATE_DIR = REPO_ROOT / "web" / "templates"
STATIC_DIR = REPO_ROOT / "web" / "static"

DISCLAIMER = "FreshGuard does not certify food safety."

SHADCN_TOKENS = [
    "--background",
    "--foreground",
    "--card",
    "--card-foreground",
    "--primary",
    "--primary-foreground",
    "--secondary",
    "--muted",
    "--muted-foreground",
    "--accent",
    "--destructive",
    "--border",
    "--input",
    "--ring",
    "--radius",
]

#: Shape tokens are deliberately theme-independent, so they are excluded from
#: the dark-mode override check below.
SHAPE_TOKENS = ["--radius"]

COLOUR_TOKENS = [name for name in SHADCN_TOKENS if name not in SHAPE_TOKENS]

STATUS_TOKENS = [
    "--status-fresh",
    "--status-fresh-foreground",
    "--status-soon",
    "--status-soon-foreground",
    "--status-check",
    "--status-check-foreground",
    "--status-fault",
    "--status-fault-foreground",
]

COMPONENT_CLASSES = [
    ".fg-btn",
    ".fg-card",
    ".fg-badge",
    ".fg-input",
    ".fg-label",
    ".fg-table",
    ".fg-alert",
    ".fg-nav",
    ".fg-grid",
    ".fg-stack",
    # Added with the workspace sections: every page opens with a page head,
    # and the pages that explain themselves use a note.
    ".fg-page-head",
    ".fg-note",
    ".fg-select",
    ".fg-lcd",
    ".fg-plan",
    ".fg-report-bar",
    ".fg-log",
]


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def root_block(css: str) -> str:
    match = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert match, ":root block not found"
    return match.group(1)


def dark_block(css: str) -> str:
    match = re.search(r"\.dark\s*\{(.*?)\}", css, re.S)
    assert match, ".dark block not found"
    return match.group(1)


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(colour: str) -> float:
    def channel(component: float) -> float:
        return component / 12.92 if component <= 0.03928 else ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(c) for c in hex_to_rgb(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def token_value(block: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", block)
    assert match, f"{name} is not defined"
    return match.group(1).strip()


class TestTokens:
    @pytest.mark.parametrize("name", SHADCN_TOKENS)
    def test_shadcn_token_is_defined(self, css: str, name: str) -> None:
        assert f"{name}:" in root_block(css)

    @pytest.mark.parametrize("name", STATUS_TOKENS)
    def test_status_token_is_defined(self, css: str, name: str) -> None:
        assert f"{name}:" in root_block(css)

    @pytest.mark.parametrize("name", [*COLOUR_TOKENS, *STATUS_TOKENS])
    def test_dark_mode_overrides_every_colour_token(self, css: str, name: str) -> None:
        assert f"{name}:" in dark_block(css)

    @pytest.mark.parametrize("name", SHAPE_TOKENS)
    def test_shape_tokens_are_shared_by_both_themes(self, css: str, name: str) -> None:
        """Shape does not change with the theme, so .dark must not redefine it."""
        assert f"{name}:" in root_block(css)
        assert f"{name}:" not in dark_block(css)

    def test_fault_is_not_green_like_fresh(self, css: str) -> None:
        """A faulty sensor must never look like a fresh item."""
        block = root_block(css)
        fresh = hex_to_rgb(token_value(block, "--status-fresh-foreground"))
        fault = hex_to_rgb(token_value(block, "--status-fault-foreground"))
        # Fresh is green dominant; fault must not be.
        assert fresh[1] > fresh[2]
        assert fault[2] > fault[1]


class TestContrast:
    @pytest.mark.parametrize("status", ["fresh", "soon", "check", "fault"])
    def test_status_badges_meet_wcag_aa(self, css: str, status: str) -> None:
        block = root_block(css)
        ratio = contrast(
            token_value(block, f"--status-{status}-foreground"),
            token_value(block, f"--status-{status}"),
        )
        assert ratio >= 4.5, f"{status} badge contrast is only {ratio:.2f}"

    @pytest.mark.parametrize("status", ["fresh", "soon", "check", "fault"])
    def test_dark_status_badges_meet_wcag_aa(self, css: str, status: str) -> None:
        block = dark_block(css)
        ratio = contrast(
            token_value(block, f"--status-{status}-foreground"),
            token_value(block, f"--status-{status}"),
        )
        assert ratio >= 4.5, f"dark {status} badge contrast is only {ratio:.2f}"

    @pytest.mark.parametrize("block_name", ["root", "dark"])
    def test_body_text_meets_wcag_aa(self, css: str, block_name: str) -> None:
        block = root_block(css) if block_name == "root" else dark_block(css)
        ratio = contrast(token_value(block, "--foreground"), token_value(block, "--background"))
        assert ratio >= 4.5, f"body text contrast is only {ratio:.2f}"

    @pytest.mark.parametrize("block_name", ["root", "dark"])
    def test_muted_text_meets_wcag_aa(self, css: str, block_name: str) -> None:
        block = root_block(css) if block_name == "root" else dark_block(css)
        ratio = contrast(
            token_value(block, "--muted-foreground"), token_value(block, "--background")
        )
        assert ratio >= 4.5, f"muted text contrast is only {ratio:.2f}"

    @pytest.mark.parametrize("block_name", ["root", "dark"])
    def test_primary_button_meets_wcag_aa(self, css: str, block_name: str) -> None:
        block = root_block(css) if block_name == "root" else dark_block(css)
        ratio = contrast(
            token_value(block, "--primary-foreground"), token_value(block, "--primary")
        )
        assert ratio >= 4.5, f"primary button contrast is only {ratio:.2f}"

    @pytest.mark.parametrize("block_name", ["root", "dark"])
    def test_focus_ring_meets_the_ui_component_threshold(self, css: str, block_name: str) -> None:
        block = root_block(css) if block_name == "root" else dark_block(css)
        ratio = contrast(token_value(block, "--ring"), token_value(block, "--background"))
        assert ratio >= 3.0, f"focus ring contrast is only {ratio:.2f}"


class TestComponents:
    @pytest.mark.parametrize("selector", COMPONENT_CLASSES)
    def test_component_class_exists(self, css: str, selector: str) -> None:
        assert re.search(rf"{re.escape(selector)}[\s,{{:]", css)

    def test_there_is_exactly_one_stylesheet(self) -> None:
        assert [p.name for p in (STATIC_DIR / "css").glob("*.css")] == ["freshguard.css"]

    def test_no_external_resources_are_fetched(self, css: str) -> None:
        """Everything is served from the Pi; the LAN has no internet."""
        assert "http://" not in css
        assert "https://" not in css
        assert "@import" not in css


class TestTemplates:
    def template_text(self, name: str) -> str:
        return (TEMPLATE_DIR / name).read_text(encoding="utf-8")

    def test_base_carries_the_safety_disclaimer(self) -> None:
        assert DISCLAIMER in self.template_text("base.html")

    def test_base_loads_only_the_shared_stylesheet(self) -> None:
        text = self.template_text("base.html")
        assert text.count('<link rel="stylesheet"') == 1
        assert "freshguard.css" in text

    def test_base_declares_a_responsive_viewport(self) -> None:
        assert 'name="viewport"' in self.template_text("base.html")

    @pytest.mark.parametrize("name", ["base.html", "components.html", "login.html"])
    def test_no_inline_styles(self, name: str) -> None:
        assert not re.search(r"\sstyle\s*=", self.template_text(name))

    @pytest.mark.parametrize("name", ["base.html", "components.html", "login.html"])
    def test_no_inline_event_handlers(self, name: str) -> None:
        assert not re.search(r"\son(click|change|submit|input|load)\s*=", self.template_text(name))

    @pytest.mark.parametrize("name", ["base.html", "components.html", "login.html"])
    def test_no_external_assets(self, name: str) -> None:
        text = self.template_text(name)
        assert "//cdn" not in text
        assert "https://" not in text

    def test_login_extends_the_base_layout(self) -> None:
        assert '{% extends "base.html" %}' in self.template_text("login.html")

    def test_login_labels_every_input(self) -> None:
        """Fields come from the field() macro, which always emits a label."""
        text = self.template_text("login.html")
        assert "field('username'" in text
        assert "field('password'" in text

    def test_password_field_is_masked(self) -> None:
        assert "type='password'" in self.template_text("login.html")

    def test_login_carries_a_csrf_token(self) -> None:
        assert "csrf_token" in self.template_text("login.html")

    def test_every_status_has_a_text_label(self) -> None:
        """Status is never communicated by colour alone."""
        text = self.template_text("components.html")
        for label in ("Fresh/Normal", "Use Soon", "Check Food", "Sensor Fault/Data Unavailable"):
            assert label in text

    def test_the_logo_is_referenced_through_the_static_helper(self) -> None:
        assert "img/logo.png" in self.template_text("base.html")
        assert (STATIC_DIR / "img" / "logo.png").is_file()


class TestRenderedLoginPage:
    @pytest.fixture(scope="class")
    def html(self, tmp_path_factory: pytest.TempPathFactory) -> str:
        from scripts.render_preview import render

        out = tmp_path_factory.mktemp("preview") / "login.html"
        return render("login", out).read_text(encoding="utf-8")

    def test_renders_without_errors(self, html: str) -> None:
        assert "<title>Sign in · FreshGuard</title>" in html

    def test_shows_the_disclaimer(self, html: str) -> None:
        assert DISCLAIMER in html

    def test_inputs_are_associated_with_labels(self, html: str) -> None:
        for name in ("username", "password"):
            assert f'for="field-{name}"' in html
            assert f'id="field-{name}"' in html

    def test_has_a_skip_link_to_the_main_content(self, html: str) -> None:
        assert 'class="fg-skip-link" href="#main"' in html
        assert 'id="main"' in html

    def test_contains_no_inline_styles(self, html: str) -> None:
        assert not re.search(r"\sstyle\s*=", html)

    def test_dark_theme_is_opt_in(self, tmp_path: Path) -> None:
        from scripts.render_preview import render

        light = render("login", tmp_path / "l.html").read_text(encoding="utf-8")
        dark = render("login", tmp_path / "d.html", dark=True).read_text(encoding="utf-8")
        assert 'class=""' in light
        assert 'class="dark"' in dark


class TestThemeSelection:
    """The theme can be stated in the address, which is how it is photographed."""

    @pytest.fixture(scope="class")
    def theme_js(self) -> str:
        return (STATIC_DIR / "js" / "theme.js").read_text(encoding="utf-8")

    def test_an_explicit_theme_in_the_url_is_honoured(self, theme_js: str) -> None:
        assert 'get("theme")' in theme_js
        assert '=== "light" || asked === "dark"' in theme_js

    def test_only_the_two_known_themes_are_accepted(self, theme_js: str) -> None:
        """Anything else falls through to the stored or system preference."""
        assert "asked || stored() || (prefersDark()" in theme_js

    def test_the_preference_is_applied_before_first_paint(self) -> None:
        base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
        head = base.split("</head>")[0]
        assert "js/theme.js" in head
