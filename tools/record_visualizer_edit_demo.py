"""Record real inline task/subtask editing against the fictional offline demo.

Requires development Playwright/Chromium and ffmpeg on PATH. No Marvin credentials
or account writes. Example: python tools/record_visualizer_edit_demo.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.visualizer_server import VisualizerServer

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/screenshots/review/editing-demo.gif"
    )
    parser.add_argument("--video-output", type=Path)
    parser.add_argument(
        "--frames-dir", type=Path, help="Optional before/during/after proof frames."
    )
    args = parser.parse_args()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        parser.error("ffmpeg is required to encode the GIF")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.frames_dir:
        args.frames_dir.mkdir(parents=True, exist_ok=True)
    raw = (ROOT / "docs/screenshots/visualizer-demo.json").read_bytes()
    plan = parse_plan_bytes(raw)
    with tempfile.TemporaryDirectory(prefix="pilot-fictional-recording-") as temporary:
        directory = Path(temporary)
        source = directory / "fictional-demo.json"
        source.write_bytes(raw)
        server = VisualizerServer(
            preloaded_plan=plan,
            source_name=source.name,
            preloaded_raw=raw,
            plan_path=source,
            allow_edit=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 960},
                        record_video_dir=directory / "recordings",
                        record_video_size={"width": 1280, "height": 960},
                        locale="en-US",
                        timezone_id="America/Los_Angeles",
                        reduced_motion="reduce",
                    )
                    page = context.new_page()
                    started = time.perf_counter()
                    video = page.video
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(server.url)
                    page.get_by_role("radio", name="After only", exact=True).click()
                    task = page.locator(
                        '.hierarchy-pane-after [data-operation-id="clarify-mobile-task"] '
                        ".task-title"
                    )
                    # Keep the edited task and the neighboring task's checklist in one frame.
                    subtask = page.locator(
                        '.hierarchy-pane-after [data-subtask-id="subtask-headline"] .subtask-title'
                    )
                    task.scroll_into_view_if_needed()
                    trim_start = time.perf_counter() - started
                    page.wait_for_timeout(700)
                    if args.frames_dir:
                        page.screenshot(path=str(args.frames_dir / "before.png"))
                    task.click()
                    task.press("End")
                    page.keyboard.type(" +4d", delay=130)
                    task.press("Enter")
                    expect(page.locator("#edit-feedback")).to_have_text(
                        "Saved for AI · Nothing applied"
                    )
                    page.wait_for_timeout(850)
                    if args.frames_dir:
                        page.screenshot(path=str(args.frames_dir / "during.png"))
                    subtask.click()
                    subtask.press("ControlOrMeta+A")
                    page.keyboard.type("Draft the opening headline", delay=65)
                    subtask.press("Enter")
                    expect(page.locator("#edit-feedback")).to_have_text(
                        "Saved for AI · Nothing applied"
                    )
                    expect(task).to_have_text("Test mobile layout and keyboard navigation +4d")
                    expect(subtask).to_have_text("Draft the opening headline")
                    assert page.locator(".text-edited").count() == 2
                    assert (
                        page.locator("#save-revision, #apply-reviewed:not([hidden])").count() == 0
                    )
                    page.wait_for_timeout(1800)
                    if args.frames_dir:
                        page.screenshot(path=str(args.frames_dir / "after.png"))
                    assert source.read_bytes() == raw
                    feedback = json.loads(next(directory.glob("*.text-review-*.json")).read_bytes())
                    assert len(feedback["edits"]) == 2
                    assert {edit["field"] for edit in feedback["edits"]} == {
                        "title",
                        "subtaskTitle",
                    }
                    assert not errors, errors
                    context.close()
                    assert video is not None
                    recording = video.path()
                    if args.video_output:
                        args.video_output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(recording, args.video_output)
                    subprocess.run(
                        [
                            ffmpeg,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(recording),
                            "-ss",
                            str(trim_start),
                            "-filter_complex",
                            "fps=8,scale=1100:-1:flags=lanczos,split[a][b];"
                            "[a]palettegen=max_colors=128:stats_mode=diff:reserve_transparent=1[p];"
                            "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
                            "-loop",
                            "0",
                            str(args.output),
                        ],
                        check=True,
                    )
                    print(f"Saved {args.output} ({args.output.stat().st_size:,} bytes)")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
