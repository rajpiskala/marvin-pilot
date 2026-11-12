"""Capture the public, fictional visualizer demo without opening a visible browser."""

from __future__ import annotations

import argparse
import tempfile
import threading
from collections import Counter
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.visualizer_server import VisualizerServer

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "screenshots" / "visualizer-demo.json"
SCREENSHOT = ROOT / "docs" / "screenshots" / "visualizer-demo.png"
SCREENSHOT_DARK = ROOT / "docs" / "screenshots" / "visualizer-demo-dark.png"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--output-dir", type=Path, default=SCREENSHOT.parent)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--review", action="store_true", help="Capture the inline-edited review state."
    )
    mode.add_argument("--before-only", action="store_true", help="Capture the unchanged baseline.")
    args = parser.parse_args()
    raw = args.plan.read_bytes()
    plan = parse_plan_bytes(raw)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="pilot-fictional-capture-")
    path = Path(temporary.name) / "fictional-demo.json"
    path.write_bytes(raw)
    options = {"preloaded_raw": raw, "plan_path": path, "allow_edit": True} if args.review else {}
    server = VisualizerServer(preloaded_plan=plan, source_name="fictional-demo.json", **options)
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
                    locale="en-US",
                    timezone_id="America/Los_Angeles",
                )
                page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(server.url)
                page.locator("#plan-view").wait_for(state="visible")
                assert page.locator("#plan-summary").inner_text() == plan.summary
                counts = Counter(operation.action for operation in plan.operations)
                for action in ("create", "update", "complete", "trash"):
                    assert page.locator(f"#{action}-count").inner_text() == str(counts[action])
                page.get_by_role("radio", name="Light theme").click()
                page.get_by_role("radio", name="Side by side").click()
                if args.review:
                    page.locator(
                        '.hierarchy-pane-after [data-operation-id="rename-website-project"] '
                        ".task-title"
                    ).fill("Website Launch v3 (2026)")
                    page.locator(
                        '.hierarchy-pane-after [data-subtask-id="subtask-headline"] .subtask-title'
                    ).fill("Draft the opening headline")
                    page.locator("#plan-summary").click()
                    expect(page.locator("#save-revision")).to_be_enabled()
                    assert page.locator("#editor-heading").count() == 0
                    assert page.locator("#apply-reviewed").is_hidden()
                    assert page.locator(".text-edited").count() == 2
                    screenshot = args.output_dir / "review-after.png"
                elif args.before_only:
                    screenshot = args.output_dir / "review-before.png"
                else:
                    screenshot = args.output_dir / SCREENSHOT.name
                page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
                print(f"Saved {screenshot}")
                if not args.review and not args.before_only:
                    page.get_by_role("radio", name="Night theme").click()
                    dark = args.output_dir / SCREENSHOT_DARK.name
                    page.screenshot(path=str(dark), full_page=True, animations="disabled")
                    print(f"Saved {dark}")
                assert path.read_bytes() == raw
                assert page_errors == [], page_errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        temporary.cleanup()


if __name__ == "__main__":
    main()
