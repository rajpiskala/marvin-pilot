"""Capture the public, fictional visualizer demo without opening a visible browser."""

from __future__ import annotations

import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.visualizer_server import VisualizerServer

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "screenshots" / "visualizer-demo.json"
SCREENSHOT = ROOT / "docs" / "screenshots" / "visualizer-demo.png"
SCREENSHOT_DARK = ROOT / "docs" / "screenshots" / "visualizer-demo-dark.png"


def main() -> None:
    plan = parse_plan_bytes(PLAN.read_bytes())
    server = VisualizerServer(
        preloaded_plan=plan,
        source_name="fictional-demo.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1600, "height": 1000},
                    device_scale_factor=2,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(server.url)
                page.locator("#plan-view").wait_for(state="visible")
                assert page.locator("#plan-summary").inner_text() == plan.summary
                assert page.locator("#create-count").inner_text() == "2"
                assert page.locator("#update-count").inner_text() == "3"
                assert page.locator("#complete-count").inner_text() == "1"
                assert page.locator("#trash-count").inner_text() == "1"
                page.get_by_role("radio", name="Light theme").click()
                page.get_by_role("radio", name="Side by side").click()
                page.screenshot(path=str(SCREENSHOT), full_page=True, animations="disabled")
                print(f"Saved {SCREENSHOT}")
                page.get_by_role("radio", name="Night theme").click()
                page.screenshot(path=str(SCREENSHOT_DARK), full_page=True, animations="disabled")
                print(f"Saved {SCREENSHOT_DARK}")
                assert page_errors == [], page_errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
