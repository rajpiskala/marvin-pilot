from __future__ import annotations

import copy
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.visualizer_server import LoadedVisualization, VisualizerServer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("MARVIN_PILOT_BROWSER_TESTS") != "1",
        reason="set MARVIN_PILOT_BROWSER_TESTS=1 to run browser tests",
    ),
]


@contextmanager
def running_visualizer(
    *, preload: bool = True, plan_dict: dict | None = None, server_kwargs: dict | None = None
):
    source = plan_dict if plan_dict is not None else EXAMPLE_PLAN
    plan = parse_plan_bytes(json.dumps(source).encode()) if preload else None
    server = VisualizerServer(
        preloaded_plan=plan,
        source_name="review-plan.json" if preload else None,
        session_token="browser-test-session",
        **(server_kwargs or {}),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()


def regression_plan(number: str) -> dict:
    corpus = os.environ.get("MARVIN_PILOT_PRIVATE_PLAN_DIR")
    if corpus:
        path = Path(corpus) / f"{number}.json"
        if not path.is_file():
            pytest.fail(f"private regression fixture {number}.json is not present")
        return json.loads(path.read_text(encoding="utf-8"))
    return synthetic_regression_plan(number)


def hierarchy_plan(*, show_day_sections: bool = False, depth: int = 2) -> dict:
    path = [
        {
            "id": f"category-{index}",
            "type": "category",
            "title": (
                "Example Workspace"
                if index == 0
                else ("Project Atlas" if index == 1 else f"Level {index}")
            ),
            **({"emoji": "💼", "color": "#c69c7b"} if index == 0 else {}),
        }
        for index in range(depth)
    ]
    before_project = {
        "id": "project-monitoring",
        "type": "project",
        "title": "Release Monitoring",
    }
    after_project = {"id": "project-monitoring", "type": "project", "title": "Monitoring"}
    waiting = {"key": "waiting", "title": "Waiting", "order": 10}
    main = {"key": "main", "title": "Main", "order": 30}
    return {
        "schemaVersion": 1,
        "planId": "44444444-4444-4444-8444-444444444444",
        "createdAt": "2026-08-13T12:00:00-07:00",
        "summary": "Preview a Marvin hierarchy cleanup.",
        "reviewDisplay": {"showDaySectionsByDefault": show_day_sections},
        "operations": [
            {
                "operationId": "rename-monitoring",
                "action": "update",
                "target": {
                    "type": "project",
                    "id": "project-monitoring",
                    "title": "Release Monitoring",
                },
                "reason": "Use the concise project name.",
                "before": {"title": "Release Monitoring"},
                "after": {"title": "Monitoring"},
                "display": {
                    "beforePath": path,
                    "afterPath": path,
                    "beforeDaySection": waiting,
                    "afterDaySection": main,
                },
            },
            {
                "operationId": "rename-task",
                "action": "update",
                "target": {"type": "task", "id": "task-a", "title": "Task A"},
                "reason": "Make the task concrete.",
                "before": {"title": "Task A"},
                "after": {"title": "My Task A"},
                "display": {
                    "beforePath": [*path, before_project],
                    "afterPath": [*path, after_project],
                    "beforeDaySection": waiting,
                    "afterDaySection": main,
                },
            },
        ],
    }


def synthetic_regression_plan(number: str) -> dict:
    workspace = {
        "id": "category-workspace",
        "type": "category",
        "title": "Example Workspace",
    }
    atlas = {"id": "category-atlas", "type": "category", "title": "Project Atlas"}
    monitoring = {
        "id": "project-monitoring",
        "type": "project",
        "title": "Release Monitoring",
    }
    followups = {"id": "project-followups", "type": "project", "title": "Follow-ups"}

    if number == "01":
        plan = hierarchy_plan(show_day_sections=True)
        path = plan["operations"][0]["display"]["beforePath"]
        plan["operations"].append(
            {
                "operationId": "move-alert-review",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-alert-review",
                    "title": "Review alert routing",
                },
                "reason": "Group follow-up work together.",
                "before": {
                    "parent": {
                        "id": "project-monitoring",
                        "title": "Release Monitoring",
                    }
                },
                "after": {"parent": {"id": "project-followups", "title": "Follow-ups"}},
                "display": {
                    "beforePath": [*path, monitoring],
                    "afterPath": [*path, followups],
                    "beforeDaySection": {"key": "waiting", "title": "Waiting", "order": 10},
                    "afterDaySection": {"key": "main", "title": "Main", "order": 30},
                },
            }
        )
        return plan

    if number == "02":
        personal = {"id": "category-personal", "type": "category", "title": "Personal"}
        learning = {"id": "category-learning", "type": "category", "title": "Learning"}
        skills = {"id": "project-skills", "type": "project", "title": "Skills"}
        study = {"id": "project-study", "type": "project", "title": "Study Plan"}
        return {
            "schemaVersion": 1,
            "planId": "77777777-7777-4777-8777-777777777777",
            "createdAt": "2026-08-13T12:00:00-07:00",
            "summary": "Move a task to an external hierarchy.",
            "operations": [
                {
                    "operationId": "move-learning-task",
                    "action": "update",
                    "target": {"type": "task", "id": "task-learning", "title": "Review notes"},
                    "reason": "Place the task with its learning material.",
                    "before": {
                        "parent": {
                            "id": "project-monitoring",
                            "title": "Release Monitoring",
                        }
                    },
                    "after": {"parent": {"id": "project-study", "title": "Study Plan"}},
                    "display": {
                        "beforePath": [workspace, atlas, monitoring],
                        "afterPath": [personal, learning, skills, study],
                    },
                }
            ],
        }

    if number == "03":
        plan = hierarchy_plan()
        plan["planId"] = "88888888-8888-4888-8888-888888888888"
        path = plan["operations"][0]["display"]["afterPath"]
        plan["operations"].append(
            {
                "operationId": "create-release-check",
                "action": "create",
                "target": {
                    "type": "task",
                    "id": "99999999-9999-4999-8999-999999999999",
                },
                "reason": "Add an explicit verification step.",
                "dependsOnOperations": ["rename-monitoring"],
                "after": {
                    "title": "Verify the release candidate",
                    "parent": {
                        "id": "project-monitoring",
                        "title": "Monitoring",
                    },
                },
                "display": {
                    "afterPath": [
                        *path,
                        {**monitoring, "title": "Monitoring"},
                    ]
                },
            }
        )
        return plan

    if number == "04":
        return {
            "schemaVersion": 1,
            "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "createdAt": "2026-08-13T12:00:00-07:00",
            "summary": "Complete a delivered synthetic project.",
            "operations": [
                {
                    "operationId": "complete-delivered-project",
                    "action": "complete",
                    "target": {
                        "type": "project",
                        "id": "project-delivered",
                        "title": "Delivered project",
                    },
                    "reason": "The final deliverable shipped.",
                    "completedAt": "2026-08-12T17:00:00-07:00",
                    "display": {
                        "beforePath": [workspace, atlas],
                        "afterPath": [workspace, atlas],
                    },
                }
            ],
        }

    raise AssertionError(f"unknown regression fixture {number}")


def project_actions_plan() -> dict:
    old_parent = {"id": "category-old", "type": "category", "title": "Old category"}
    new_parent = {"id": "category-new", "type": "category", "title": "New category"}
    return {
        "schemaVersion": 1,
        "planId": "66666666-6666-4666-8666-666666666666",
        "createdAt": "2026-08-13T12:00:00-07:00",
        "summary": "Preview every project action.",
        "operations": [
            {
                "operationId": "create-project",
                "action": "create",
                "target": {"type": "project", "id": "77777777-7777-4777-8777-777777777777"},
                "reason": "Create a focused project.",
                "after": {
                    "title": "Created project",
                    "parent": {"id": "category-new", "title": "New category"},
                    "scheduledDate": "2026-08-14",
                },
                "display": {"afterPath": [new_parent]},
            },
            {
                "operationId": "update-project",
                "action": "update",
                "target": {"type": "project", "id": "project-update", "title": "Old project"},
                "reason": "Rename, move, and schedule the project.",
                "before": {
                    "title": "Old project",
                    "parent": {"id": "category-old", "title": "Old category"},
                    "scheduledDate": None,
                },
                "after": {
                    "title": "Updated project",
                    "parent": {"id": "category-new", "title": "New category"},
                    "scheduledDate": "2026-08-15",
                },
                "display": {"beforePath": [old_parent], "afterPath": [new_parent]},
            },
            {
                "operationId": "complete-project",
                "action": "complete",
                "target": {
                    "type": "project",
                    "id": "project-complete",
                    "title": "Completed project",
                },
                "reason": "Record the historical completion.",
                "completedAt": "2026-08-12T17:00:00-07:00",
                "display": {"beforePath": [old_parent], "afterPath": [old_parent]},
            },
            {
                "operationId": "trash-project",
                "action": "trash",
                "target": {
                    "type": "project",
                    "id": "project-trash",
                    "title": "Redundant project",
                },
                "reason": "Remove the redundant project through Trash.",
                "display": {"beforePath": [old_parent]},
            },
        ],
    }


def subtask_conversion_plan() -> dict:
    work = {
        "id": "category-work",
        "type": "category",
        "title": "Work",
        "emoji": "💼",
        "color": "#c69c7b",
    }
    project = {"id": "project-dinner", "type": "project", "title": "Dinner"}
    return {
        "schemaVersion": 1,
        "planId": "88888888-8888-4888-8888-888888888888",
        "createdAt": "2026-08-14T08:00:00-07:00",
        "summary": "Preview ordered subtask consolidation.",
        "operations": [
            {
                "operationId": "build-dinner-checklist",
                "action": "update",
                "target": {"type": "task", "id": "parent-dinner", "title": "Handle dinner"},
                "reason": "Put the dinner workflow in one ordered checklist.",
                "before": {
                    "subtasks": [
                        {"id": "pickup", "title": "Pick up food"},
                        {"id": "obsolete", "title": "Check coupons"},
                    ]
                },
                "after": {
                    "subtasks": [
                        {
                            "id": "order",
                            "title": "(1) Order food",
                            "sourceTask": {
                                "id": "loose-order",
                                "title": "Order food",
                                "acceptLoss": ["estimatedTimeDuration"],
                            },
                        },
                        {"id": "pickup", "title": "(2) Pick up food", "done": True},
                        {"id": "check", "title": "(3) Check the food is correct"},
                    ]
                },
                "display": {
                    "beforePath": [work, project],
                    "afterPath": [work, project],
                },
            },
            {
                "operationId": "trash-loose-order",
                "action": "trash",
                "target": {"type": "task", "id": "loose-order", "title": "Order food"},
                "reason": "The new subtask replaces this loose task.",
                "dependsOnOperations": ["build-dinner-checklist"],
                "display": {"beforePath": [work]},
            },
        ],
    }


def ordered_same_target_plan() -> dict:
    return {
        "schemaVersion": 1,
        "planId": "99999999-9999-4999-8999-999999999999",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "Preview an ordered same-task chain.",
        "operations": [
            {
                "operationId": "rename-release",
                "action": "update",
                "target": {"type": "task", "id": "release", "title": "Draft release"},
                "reason": "Use the final title.",
                "before": {"title": "Draft release"},
                "after": {"title": "Publish release"},
            },
            {
                "operationId": "schedule-release",
                "action": "update",
                "target": {"type": "task", "id": "release", "title": "Publish release"},
                "reason": "Record the delivery date.",
                "dependsOnOperations": ["rename-release"],
                "before": {"scheduledDate": None},
                "after": {"scheduledDate": "2026-08-08"},
            },
            {
                "operationId": "complete-release",
                "action": "complete",
                "target": {"type": "task", "id": "release", "title": "Publish release"},
                "reason": "Close the delivered work.",
                "dependsOnOperations": ["schedule-release"],
                "completedAt": "2026-08-08T17:00:00-07:00",
            },
        ],
    }


@pytest.fixture(scope="module")
def browser():
    playwright_module = pytest.importorskip("playwright.sync_api")
    with playwright_module.sync_playwright() as playwright:
        engine_name = os.environ.get("MARVIN_PILOT_BROWSER_ENGINE", "chromium")
        if engine_name not in {"chromium", "firefox"}:
            pytest.fail(f"unsupported MARVIN_PILOT_BROWSER_ENGINE: {engine_name}")
        instance = getattr(playwright, engine_name).launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    yield page
    assert page_errors == []
    context.close()


def test_preloaded_plan_themes_views_and_preferences(page) -> None:
    with running_visualizer() as server:
        responses = []
        page.on("response", lambda response: responses.append(response.url))
        page.goto(server.url)

        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator("#plan-summary").inner_text() == EXAMPLE_PLAN["summary"]
        assert page.title() == f"Marvin Pilot - {EXAMPLE_PLAN['summary']}"
        assert page.locator(".hierarchy-preview").count() == 1
        assert page.locator(".operation-row").count() == 6
        assert page.locator(".diff-row").count() == 0
        assert page.locator(".hierarchy-pane-before .task-card").count() == 3
        assert page.locator(".hierarchy-pane-after .task-card").count() == 3
        assert page.locator(".hierarchy-context .context-title").all_inner_texts() == [
            "Household",
            "Inbox",
            "Math",
            "Household",
            "People",
            "Operations",
        ]
        assert page.locator(".comparison-row, .empty-state, .operation-heading").count() == 0
        assert page.locator(".preview-pane-caption, .section-counts").count() == 0
        assert page.locator(".topbar-title").inner_text() == "Marvin Pilot"
        assert page.locator(".brand img").get_attribute("src") == "marvin-pilot.png"
        assert page.locator("#open-another").inner_text() == "📁 Open plan"
        assert page.locator(
            '[data-operation-id="reschedule-wash-dishes"] .task-item'
        ).all_inner_texts() == ["+2026-08-08", "+2026-08-09"]
        assert page.locator(
            '[data-operation-id="improve-dinner-task"] .task-item.estimate'
        ).all_inner_texts() == ["~None", "~4h30m"]
        assert all(url.startswith(server.url) for url in responses)

        native_metrics = page.locator(".task-card").first.evaluate(
            """element => {
                const style = getComputedStyle(element);
                const titleStyle = getComputedStyle(element.querySelector(".task-title"));
                    const icon = element.closest(".hierarchy-node-row")
                        ?.querySelector(":scope > .hierarchy-icon-slot .object-icon")
                        || element.querySelector(".object-icon");
                const iconStyle = getComputedStyle(icon);
                return {
                    padding: style.padding,
                    radius: style.borderRadius,
                    background: style.backgroundColor,
                    height: element.getBoundingClientRect().height,
                    titleFontSize: titleStyle.fontSize,
                    titleFontWeight: titleStyle.fontWeight,
                    titleLineHeight: titleStyle.lineHeight,
                        iconLabel: icon.getAttribute("aria-label"),
                        iconSize: iconStyle.width,
                        boxShadow: style.boxShadow,
                };
            }"""
        )
        native_height = native_metrics.pop("height")
        native_box_shadow = native_metrics.pop("boxShadow")
        assert 55 <= native_height <= 59
        assert "rgb(142, 184, 255)" in native_box_shadow
        assert native_metrics == {
            "padding": "17px 17px 17px 12px",
            "radius": "0px 9px 9px 0px",
            "background": "rgb(255, 255, 255)",
            "titleFontSize": "14px",
            "titleFontWeight": "500",
            "titleLineHeight": "21px",
            "iconLabel": "Task",
            "iconSize": "15px",
        }
        assert page.locator(".sparse-label").count() == 0
        action_edges = page.locator(
            ".action-trash .task-card, .action-create .task-card"
        ).evaluate_all("elements => elements.map(element => getComputedStyle(element).boxShadow)")
        assert any("rgb(123, 220, 165)" in shadow for shadow in action_edges)
        assert any("rgb(255, 145, 153)" in shadow for shadow in action_edges)

        page.get_by_role("radio", name="Light theme").focus()
        page.keyboard.press("ArrowRight")
        page.get_by_role("radio", name="Side by side").focus()
        page.keyboard.press("ArrowRight")
        assert page.locator("html").get_attribute("data-theme") == "dusk"
        page.get_by_role("radio", name="Night theme").click()
        assert page.locator("html").get_attribute("data-theme") == "night"
        page.get_by_role("radio", name="Dusk theme").click()
        assert page.locator(".hierarchy-preview-before").count() == 1
        assert page.locator(".hierarchy-pane").count() == 1
        assert page.locator(".hierarchy-pane .preview-pane-header h2").inner_text() == "Now"
        assert page.locator(".operation-row").count() == 3
        assert page.locator(".context-title").all_inner_texts() == [
            "Household",
            "Inbox",
            "Math",
        ]

        page.reload()
        page.locator("#plan-view").wait_for(state="visible")
        assert page.get_by_role("radio", name="Dusk theme").get_attribute("aria-checked") == "true"
        assert page.get_by_role("radio", name="Now only").get_attribute("aria-checked") == "true"

        page.get_by_role("radio", name="After only").click()
        assert page.locator(".hierarchy-pane .preview-pane-header h2").inner_text() == (
            "After (preview)"
        )
        assert page.locator(".context-title").all_inner_texts() == [
            "Household",
            "People",
            "Operations",
        ]

        storage = page.evaluate("Object.keys(window.localStorage).sort()")
        assert storage == [
            "marvinPilot.visualizer.comparisonView.v1",
            "marvinPilot.visualizer.theme.v1",
        ]


def test_drop_and_file_upload_are_validated_and_untrusted_text_stays_text(page) -> None:
    with running_visualizer(preload=False) as server:
        page.goto(server.url)
        assert page.locator("#landing").is_visible()
        theme_box = page.locator(".theme-control").bounding_box()
        brand_box = page.locator(".topbar-title").bounding_box()
        assert theme_box is not None and brand_box is not None
        assert theme_box["x"] > 1000
        assert brand_box["x"] + brand_box["width"] / 2 == pytest.approx(720, abs=2)
        unexpected_scroll_regions = page.evaluate(
            """[...document.querySelectorAll("body *")].filter(element => {
                const style = getComputedStyle(element);
                const scrollable = [style.overflow, style.overflowX, style.overflowY]
                    .some(value => value === "auto" || value === "scroll");
                return scrollable && (
                    element.scrollWidth > element.clientWidth + 1 ||
                    element.scrollHeight > element.clientHeight + 1
                );
            }).map(element => element.className || element.id || element.tagName)"""
        )
        assert unexpected_scroll_regions == []
        assert "No persistent plan storage" in page.locator(".safety-points").inner_text()
        filename = "review <img onerror=alert(1)>.json"
        page.evaluate(
            """({filename, content}) => {
                const file = new File([content], filename, {type: "application/json"});
                const transfer = new DataTransfer();
                transfer.items.add(file);
                document.querySelector("#drop-zone").dispatchEvent(
                    new DragEvent("drop", {bubbles: true, dataTransfer: transfer})
                );
            }""",
            {"filename": filename, "content": json.dumps(EXAMPLE_PLAN)},
        )

        page.locator("#plan-view").wait_for(state="visible")
        page.get_by_text("Plan information", exact=True).click()
        assert page.locator("#plan-file-name").inner_text() == filename
        assert page.locator("#plan-hierarchy-source").inner_text() == (
            "Plan metadata and references only"
        )
        assert page.locator("script").count() == 1
        assert page.locator("#plan-id").inner_text() == EXAMPLE_PLAN["planId"]

        page.locator("#plan-file").set_input_files(
            {
                "name": "invalid.json",
                "mimeType": "application/json",
                "buffer": b'{"schemaVersion":1}',
            }
        )
        page.locator("#error-panel").wait_for(state="visible")
        assert "plan schema validation failed" in page.locator("#error-message").inner_text()
        assert page.locator("#plan-summary").inner_text() == EXAMPLE_PLAN["summary"]


def test_applied_receipt_state_uses_historical_labels(page) -> None:
    with running_visualizer(
        server_kwargs={
            "review_state": "applied",
            "receipt_id": "11111111-1111-4111-8111-111111111111",
        }
    ) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator("#review-status").inner_text() == "Applied plan — receipt verified"
        assert "11111111" in page.locator("#review-status").get_attribute("title")
        headings = page.locator(".hierarchy-pane .preview-pane-header h2").all_inner_texts()
        assert headings == ["Before", "Applied result"]

        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-pane-headings h2").all_inner_texts() == [
            "Before",
            "Applied result",
        ]


def test_title_copy_mode_excludes_metadata_and_full_mode_restores_native_copy(page) -> None:
    with running_visualizer() as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        copied = page.evaluate(
            """() => {
                const titles = document.querySelectorAll('#sections [data-copy-title]');
                const range = document.createRange();
                range.setStartBefore(titles[0]);
                range.setEndAfter(titles[1]);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                const transfer = new DataTransfer();
                let captured = '';
                document.addEventListener('copy', (copyEvent) => {
                    captured = copyEvent.clipboardData.getData('text/plain');
                }, { once: true });
                const event = new ClipboardEvent('copy', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: transfer,
                });
                document.querySelector('#sections').dispatchEvent(event);
                return { text: captured, prevented: event.defaultPrevented };
            }"""
        )
        assert copied["prevented"] is True
        assert copied["text"].splitlines() == ["Wash the dishes", "Eat dinner with Jacob"]
        assert "UPDATE" not in copied["text"]
        assert (
            page.locator(".action-badge").first.evaluate(
                "element => getComputedStyle(element).userSelect"
            )
            == "none"
        )

        page.locator("#selection-mode").select_option("full")
        native = page.evaluate(
            """() => {
                const transfer = new DataTransfer();
                const event = new ClipboardEvent('copy', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: transfer,
                });
                document.querySelector('#sections').dispatchEvent(event);
                return event.defaultPrevented;
            }"""
        )
        assert native is False
        assert (
            page.locator(".action-badge").first.evaluate(
                "element => getComputedStyle(element).userSelect"
            )
            == "auto"
        )


def test_compact_titles_expand_and_single_views_remain_full(page) -> None:
    plan = copy.deepcopy(EXAMPLE_PLAN)
    long_title = " ".join(["A deliberately long task title for dense cleanup review"] * 12)
    plan["operations"][0]["target"]["title"] = long_title
    plan["operations"][0]["before"]["title"] = long_title
    plan["operations"][0]["after"]["title"] = long_title + " updated"
    with running_visualizer(plan_dict=plan) as server:
        page.set_viewport_size({"width": 900, "height": 900})
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        title = page.locator(
            '.hierarchy-pane-before [data-operation-id="reschedule-wash-dishes"] .task-title'
        )
        button = title.locator("xpath=following-sibling::button[contains(@class, 'title-expand')]")
        assert button.is_visible()
        compact_height = title.bounding_box()["height"]
        button.click()
        assert title.bounding_box()["height"] > compact_height

        page.get_by_role("radio", name="Now only").click()
        single_title = page.locator(
            '.hierarchy-pane-before [data-operation-id="reschedule-wash-dishes"] .task-title'
        )
        assert (
            single_title.locator(
                "xpath=following-sibling::button[contains(@class, 'title-expand')]"
            ).count()
            == 0
        )
        assert single_title.bounding_box()["height"] > compact_height


def test_filters_details_and_narrow_split_layout(page) -> None:
    with running_visualizer() as server:
        page.add_init_script(
            """window.localStorage.setItem("marvinPilot.visualizer.theme.v1", "broken");
            window.localStorage.setItem(
                "marvinPilot.visualizer.comparisonView.v1", "broken"
            );"""
        )
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator("html").get_attribute("data-theme") == "light"
        assert (
            page.get_by_role("radio", name="Side by side").get_attribute("aria-checked") == "true"
        )
        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == 4

        update_filter = page.locator('[data-action-filter="update"]')
        create_filter = page.locator('[data-action-filter="create"]')
        trash_filter = page.locator('[data-action-filter="trash"]')
        update_filter.click()
        assert page.locator(".operation-row").count() == 2
        assert update_filter.get_attribute("aria-pressed") == "true"
        assert create_filter.get_attribute("aria-pressed") == "false"
        assert trash_filter.get_attribute("aria-pressed") == "false"
        update_filter.click()
        assert page.locator(".operation-row").count() == 4
        assert all(
            button.get_attribute("aria-pressed") == "true"
            for button in (create_filter, update_filter, trash_filter)
        )
        trash_filter.click()
        assert page.locator(".operation-row").count() == 1
        trash_filter.click()
        moved_filter = page.locator("#moved-filter")
        moved_filter.click()
        assert page.locator(".operation-row").count() == int(
            page.locator("#moved-count").inner_text()
        )
        assert moved_filter.get_attribute("aria-pressed") == "true"
        assert all(
            button.get_attribute("aria-pressed") == "false"
            for button in (create_filter, update_filter, trash_filter)
        )
        create_filter.click()
        assert page.locator(".operation-row").count() == 1
        assert moved_filter.get_attribute("aria-pressed") == "false"
        assert create_filter.get_attribute("aria-pressed") == "true"
        assert update_filter.get_attribute("aria-pressed") == "false"
        assert trash_filter.get_attribute("aria-pressed") == "false"
        create_filter.click()
        assert page.locator(".operation-row").count() == 4
        moved_filter.click()
        moved_filter.click()
        assert page.locator(".operation-row").count() == 4
        assert moved_filter.get_attribute("aria-pressed") == "false"
        assert all(
            button.get_attribute("aria-pressed") == "true"
            for button in (create_filter, update_filter, trash_filter)
        )
        page.locator(".operation-details summary").first.click()
        assert page.locator(".operation-details[open]").count() == 1
        assert "scheduledDate" in page.locator(".diff-table").first.inner_text()

        page.emulate_media(media="print")
        assert page.locator(".topbar").evaluate("el => getComputedStyle(el).display") == "none"
        assert (
            page.locator(".operation-details .detail-body").first.evaluate(
                "el => getComputedStyle(el).display"
            )
            == "block"
        )
        page.emulate_media(media="screen")

        page.set_viewport_size({"width": 500, "height": 900})
        before = page.locator(".diff-row").first.locator(".diff-cell-before").bounding_box()
        after = page.locator(".diff-row").first.locator(".diff-cell-after").bounding_box()
        assert before is not None and after is not None
        assert before["y"] < after["y"]
        task_line = page.locator(".task-line").first.bounding_box()
        task_items = page.locator(".task-items").first.bounding_box()
        assert task_line is not None and task_items is not None
        assert task_line["width"] > 350
        assert task_items["y"] >= task_line["y"] + task_line["height"]
        no_horizontal_overflow = page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        assert no_horizontal_overflow


def test_split_rows_and_following_sections_share_geometry(page) -> None:
    plan = copy.deepcopy(EXAMPLE_PLAN)
    plan["operations"][0]["before"]["title"] = "Wash the dishes"
    plan["operations"][0]["after"]["title"] = (
        "9:00am Wash every dish, dry the cookware, put everything away, and wipe down "
        "the counters before leaving the kitchen"
    )
    with running_visualizer(plan_dict=plan) as server:
        page.set_viewport_size({"width": 900, "height": 900})
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        page.get_by_role("radio", name="Changes").click()

        first_row = page.locator(".diff-row").first
        left = first_row.locator(".diff-cell-before").bounding_box()
        right = first_row.locator(".diff-cell-after").bounding_box()
        left_card = first_row.locator(".diff-cell-before .task-card").bounding_box()
        right_card = first_row.locator(".diff-cell-after .task-card").bounding_box()
        sections = page.locator(".diff-section")
        first_section = sections.nth(0).bounding_box()
        second_section = sections.nth(1).bounding_box()

        assert all(box is not None for box in (left, right, left_card, right_card))
        assert left["y"] == pytest.approx(right["y"], abs=1)
        assert left["height"] < right["height"]
        assert left_card["height"] < right_card["height"]
        assert left_card["width"] == pytest.approx(right_card["width"], abs=1)
        assert left_card["height"] > 55
        assert first_section is not None and second_section is not None
        assert second_section["y"] >= first_section["y"] + first_section["height"]


def test_project_completion_is_visible_and_filterable(page) -> None:
    plan = {
        "schemaVersion": 1,
        "planId": "55555555-5555-4555-8555-555555555555",
        "createdAt": "2026-08-11T12:00:00-07:00",
        "summary": "Preview a historical project completion.",
        "operations": [
            {
                "operationId": "complete-project",
                "action": "complete",
                "target": {
                    "type": "project",
                    "id": "project-existing",
                    "title": "Delivered project",
                },
                "reason": "The final deliverable shipped on July 23.",
                "completedAt": "2026-07-23T18:30:00-07:00",
            }
        ],
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator("#complete-count").inner_text() == "1"
        assert page.locator(".action-complete").count() == 2
        assert page.locator(".action-complete .task-card").count() == 2
        assert page.locator(".action-complete .project-flag").count() == 2
        completion_time = page.locator(
            '.hierarchy-pane-after [data-operation-id="complete-project"] .completion-time'
        )
        assert completion_time.count() == 1
        assert completion_time.get_attribute("datetime") == "2026-07-23T18:30:00-07:00"
        expected_local = page.evaluate(
            """value => new Intl.DateTimeFormat(undefined, {
                year: "numeric", month: "short", day: "numeric",
                hour: "numeric", minute: "2-digit",
            }).format(new Date(value))""",
            "2026-07-23T18:30:00-07:00",
        )
        assert completion_time.inner_text() == f"Done {expected_local}"
        history_day = page.locator(
            '.hierarchy-pane-after [data-operation-id="complete-project"] .completion-day'
        )
        assert history_day.inner_text() == "History 2026-07-23"

        page.locator(".operation-details summary").first.click()
        details = page.locator(".operation-details[open]").inner_text()
        assert "Project ID: project-existing" in details
        assert "Completed at 2026-07-23T18:30:00-07:00" in details

        page.locator('[data-action-filter="complete"]').click()
        assert page.locator(".action-complete").count() == 2


def test_project_completion_history_repair_is_explicit_in_preview(page) -> None:
    plan = {
        "schemaVersion": 1,
        "planId": "55555555-5555-4555-8555-555555555557",
        "createdAt": "2026-09-05T12:00:00-07:00",
        "summary": "Preview one guarded project history repair.",
        "operations": [
            {
                "operationId": "repair-project-history",
                "action": "complete",
                "target": {
                    "type": "project",
                    "id": "project-history",
                    "title": "Finished project",
                },
                "reason": "Restore the missing native completion timestamp.",
                "completedAt": "2026-07-18T18:30:00-07:00",
                "completionDay": {
                    "before": None,
                    "after": "2026-07-18",
                    "behavior": "assigned",
                },
                "repairHistory": True,
            }
        ],
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        before = page.locator('.hierarchy-pane-before [data-operation-id="repair-project-history"]')
        after = page.locator('.hierarchy-pane-after [data-operation-id="repair-project-history"]')
        assert before.get_by_text("HISTORY REPAIRED").count() == 1
        assert before.locator(".completion-time").count() == 0
        assert after.get_by_text("HISTORY REPAIRED").count() == 1
        assert after.locator(".completion-time").count() == 1
        assert after.locator(".completion-day").inner_text() == "History 2026-07-18"

        page.locator(".operation-details summary").first.click()
        details = page.locator(".operation-details[open]").inner_text()
        assert "Completed; native timestamp missing" in details
        assert "Completed at 2026-07-18T18:30:00-07:00" in details


def test_task_completion_shows_history_day_and_replacement_semantics(page) -> None:
    plan = {
        "schemaVersion": 1,
        "planId": "55555555-5555-4555-8555-555555555556",
        "createdAt": "2026-09-05T12:00:00-07:00",
        "summary": "Preview a historical task completion.",
        "operations": [
            {
                "operationId": "complete-task-history",
                "action": "complete",
                "target": {
                    "type": "task",
                    "id": "task-history",
                    "title": "Archive the records",
                },
                "reason": "Record the task under its actual completion day.",
                "completedAt": "2026-09-04T18:30:00-07:00",
                "completionDay": {
                    "before": "2026-09-07",
                    "after": "2026-09-04",
                    "behavior": "replaced",
                },
            }
        ],
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        history_day = page.locator(
            '.hierarchy-pane-after [data-operation-id="complete-task-history"] .completion-day'
        )
        assert history_day.count() == 1
        assert history_day.inner_text() == "History 2026-09-04"
        assert history_day.get_attribute("datetime") == "2026-09-04"
        assert "replaced" in history_day.get_attribute("title")
        before = page.locator('.hierarchy-pane-before [data-operation-id="complete-task-history"]')
        assert before.locator(".completion-day").count() == 0
        assert before.locator(".task-items").count() == 0

        page.locator(".operation-details summary").first.click()
        details = page.locator(".operation-details[open]").inner_text()
        assert "Completion history day" in details
        assert "2026-09-07" in details
        assert "2026-09-04" in details


def test_completed_task_move_is_visibly_completed_on_both_sides(page) -> None:
    completed_at = "2026-07-23T18:30:00-07:00"
    category = {"id": "category-work", "type": "category", "title": "Work"}
    old_project = {"id": "project-old", "type": "project", "title": "Project Alpha"}
    new_project = {"id": "project-new", "type": "project", "title": "Project Beta"}
    plan = {
        "schemaVersion": 1,
        "planId": "1f7c28a6-6bbd-4629-93c3-73517834a169",
        "createdAt": "2026-08-18T12:00:00-07:00",
        "summary": "Move completed history without reopening it.",
        "operations": [
            {
                "operationId": "move-completed-task",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-completed",
                    "title": "Prepare release notes",
                },
                "reason": "Group the historical work under its durable project.",
                "before": {"parent": {"id": "project-old", "title": "Project Alpha"}},
                "after": {"parent": {"id": "project-new", "title": "Project Beta"}},
                "display": {
                    "beforePath": [category, old_project],
                    "afterPath": [category, new_project],
                    "existingCompletedAt": completed_at,
                },
            }
        ],
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        expected_local = page.evaluate(
            """value => new Intl.DateTimeFormat(undefined, {
                year: "numeric", month: "short", day: "numeric",
                hour: "numeric", minute: "2-digit",
            }).format(new Date(value))""",
            completed_at,
        )
        for pane in ("before", "after"):
            operation = page.locator(
                f'.hierarchy-pane-{pane} [data-operation-id="move-completed-task"]'
            )
            completion = operation.locator(".completion-time")
            assert completion.count() == 1
            assert completion.get_attribute("datetime") == completed_at
            assert completion.inner_text() == f"Done {expected_local}"
            completed_icon = operation.locator(
                '.task-circle.completed[aria-label="Completed task"]'
            )
            assert completed_icon.count() == 1
            assert "MOVED" in operation.locator(".action-badge").inner_text()

        page.get_by_role("radio", name="Changes").click()
        row = page.locator('[data-operation-id="move-completed-task"].diff-row')
        assert row.locator(".completion-time").count() == 2
        assert row.locator(".task-circle.completed").count() == 2


def test_every_project_action_is_a_first_class_hierarchy_row(page) -> None:
    with running_visualizer(plan_dict=project_actions_plan()) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator("#create-count").inner_text() == "1"
        assert page.locator("#update-count").inner_text() == "1"
        assert page.locator("#complete-count").inner_text() == "1"
        assert page.locator("#trash-count").inner_text() == "1"
        assert page.locator(".project-flag").count() == 6
        assert page.locator(".action-create.operation-row").count() == 1
        assert page.locator(".action-update.operation-row").count() == 2
        assert page.locator(".action-complete.operation-row").count() == 2
        assert page.locator(".action-trash.operation-row").count() == 1

        page.locator('[data-operation-id="create-project"]').click()
        no_now = page.get_by_role("button", name="No Now state")
        assert no_now.is_disabled()

        page.locator(
            '[data-operation-id="update-project"] .operation-details summary'
        ).first.click()
        details = page.locator(
            '[data-operation-id="update-project"] .operation-details[open]'
        ).first
        assert "Old category" in details.inner_text()
        assert "New category" in details.inner_text()
        assert "scheduledDate" in details.inner_text()

        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == 4


def test_marvin_hierarchy_icons_day_sections_and_counterpart_highlighting(page) -> None:
    with running_visualizer(plan_dict=hierarchy_plan()) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        assert page.get_by_role("radio", name="Preview").get_attribute("aria-checked") == "true"
        assert page.locator("#day-sections-toggle").get_attribute("aria-pressed") == "false"
        assert page.locator(".day-section").count() == 0
        assert page.locator(".category-folder").count() == 4
        assert page.locator(".project-flag").count() == 2
        assert page.locator(".task-circle").count() == 2
        assert page.locator(".context-title").all_inner_texts() == [
            "Example Workspace",
            "Project Atlas",
            "Example Workspace",
            "Project Atlas",
        ]
        assert (
            page.locator(
                '.hierarchy-pane-before [data-operation-id="rename-monitoring"] .task-title'
            ).inner_text()
            == "Release Monitoring"
        )
        assert (
            page.locator(
                '.hierarchy-pane-after [data-operation-id="rename-monitoring"] .task-title'
            ).inner_text()
            == "Monitoring"
        )

        page.locator("#day-sections-toggle").click()
        assert page.locator("#day-sections-toggle").get_attribute("aria-pressed") == "true"
        assert page.locator(".day-section-title").all_inner_texts() == ["Waiting", "Main"]
        assert page.locator(".day-section-count").all_inner_texts() == ["2 changes", "2 changes"]

        first_rename = page.locator('[data-operation-id="rename-monitoring"]').first
        first_rename.hover()
        assert (
            page.locator('[data-operation-id="rename-monitoring"].counterpart-highlight').count()
            == 2
        )

        work_branches = page.locator('.hierarchy-branch[data-node-key="category:category-0"]')
        assert work_branches.count() == 2
        work_branches.first.locator(":scope > .hierarchy-node-row .hierarchy-context").click()
        assert all(
            work_branches.nth(index).locator(":scope > .hierarchy-children").is_hidden()
            for index in range(work_branches.count())
        )

        page.get_by_role("radio", name="Changes").click()
        assert page.locator("#day-sections-toggle").is_hidden()
        assert page.locator(".diff-row").count() == 2


def test_ordered_same_target_chain_shows_boundary_preview_and_all_steps(page) -> None:
    with running_visualizer(plan_dict=ordered_same_target_plan()) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        assert page.locator(".hierarchy-pane-before .operation-row").count() == 1
        assert page.locator(".hierarchy-pane-after .operation-row").count() == 1
        assert page.locator(".hierarchy-pane-before .task-title").inner_text() == "Draft release"
        assert page.locator(".hierarchy-pane-after .task-title").inner_text() == "Publish release"
        assert page.locator(".hierarchy-pane-before .target-chain-badge").inner_text() == "STEP 1/3"
        assert page.locator(".hierarchy-pane-after .target-chain-badge").inner_text() == "STEP 3/3"

        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == 3
        assert page.locator(".target-chain-badge").all_inner_texts() == [
            "STEP 1/3",
            "STEP 1/3",
            "STEP 2/3",
            "STEP 2/3",
            "STEP 3/3",
            "STEP 3/3",
        ]
        page.locator('[data-operation-id="schedule-release"] .operation-details summary').click()
        assert (
            "same-item step 2/3"
            in page.locator('[data-operation-id="schedule-release"] .detail-body').inner_text()
        )


def test_regression_01_has_exact_cross_pane_indentation_and_move_navigation(page) -> None:
    plan = regression_plan("01")
    renamed_project = next(
        operation
        for operation in plan["operations"]
        if operation["action"] == "update"
        and operation["target"]["type"] == "project"
        and operation.get("before", {}).get("title") != operation.get("after", {}).get("title")
    )
    moved_task = next(
        operation
        for operation in plan["operations"]
        if operation["action"] == "update"
        and operation["target"]["type"] == "task"
        and operation.get("before", {}).get("parent") != operation.get("after", {}).get("parent")
    )
    with running_visualizer(plan_dict=plan) as server:
        page.set_viewport_size({"width": 1692, "height": 1100})
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        before = page.locator(".hierarchy-pane-before")
        after = page.locator(".hierarchy-pane-after")
        assert before.locator(".context-title").count() > 0
        assert after.locator(".context-title").count() > 0
        assert page.locator(".unknown-context, .hierarchy-metadata-warning").count() == 0
        assert before.locator(".operation-row").count() == sum(
            operation["action"] != "create" for operation in plan["operations"]
        )
        assert after.locator(".operation-row").count() == sum(
            operation["action"] != "trash" for operation in plan["operations"]
        )

        offsets = []
        for side in ("before", "after"):
            offsets.append(
                page.locator(
                    f'.hierarchy-pane-{side} [data-operation-id="{renamed_project["operationId"]}"]'
                ).evaluate(
                    """row => {
                        const scroll = row.closest('.hierarchy-scroll').getBoundingClientRect();
                        const title = row.querySelector('.task-title').getBoundingClientRect();
                        return Math.round(title.x - scroll.x);
                    }"""
                )
            )
        assert offsets[0] == pytest.approx(offsets[1], abs=1)

        move_badges = page.locator(
            f'[data-operation-id="{moved_task["operationId"]}"] .action-badge'
        ).all_inner_texts()
        assert len(move_badges) == 2
        assert all("MOVED" in badge for badge in move_badges)

        # A far-moving item remains understandable without vertically aligning its two homes.
        moved_row = page.locator(f'[data-operation-id="{moved_task["operationId"]}"]').first
        moved_row.scroll_into_view_if_needed()
        moved_row.click()
        tray = page.locator("#comparison-tray")
        assert tray.is_visible()
        assert "moved items" in tray.locator(".comparison-tray-title").inner_text()
        assert moved_task["after"]["parent"]["title"] in tray.inner_text()
        tray.get_by_role("button", name="Jump to After").wait_for(state="visible")

        page.get_by_role("radio", name="Changes").click()
        assert page.locator("#changes-grouping-control").is_visible()
        assert page.locator(".diff-row").count() == len(plan["operations"])
        assert (
            "after location" in page.locator(".hierarchy-group-header").first.inner_text().lower()
        )
        assert page.locator('.hierarchy-group-header [aria-label="Category"]').count() > 0
        page.locator("#changes-search").fill(moved_task["target"]["title"])
        assert page.locator(".diff-row").count() > 0
        page.locator("#changes-search").fill("")
        page.locator("#moved-filter").click()
        assert page.locator(".diff-row").count() == int(page.locator("#moved-count").inner_text())
        page.locator("#moved-filter").click()
        page.locator("#changes-grouping").select_option("before")
        assert "now location" in page.locator(".hierarchy-group-header").first.inner_text().lower()
        day_option = page.locator('#changes-grouping option[value="day"]')
        assert not day_option.is_disabled()
        page.locator("#changes-grouping").select_option("day")
        assert "today section" in page.locator(".hierarchy-group-header").first.inner_text().lower()


def test_regression_02_preserves_external_destination_and_regresses_ui(page) -> None:
    plan = regression_plan("02")
    moved = next(
        operation
        for operation in plan["operations"]
        if operation["action"] == "update"
        and operation.get("before", {}).get("parent") != operation.get("after", {}).get("parent")
    )
    with running_visualizer(plan_dict=plan) as server:
        page.set_viewport_size({"width": 1692, "height": 1100})
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        before = page.locator(".hierarchy-pane-before")
        after = page.locator(".hierarchy-pane-after")
        assert before.locator(".operation-row").count() == sum(
            operation["action"] != "create" for operation in plan["operations"]
        )
        assert after.locator(".operation-row").count() == sum(
            operation["action"] != "trash" for operation in plan["operations"]
        )
        assert page.locator(".unknown-context, .hierarchy-metadata-warning").count() == 0
        assert after.locator(".context-title").count() > before.locator(".context-title").count()
        moved_row = after.locator(f'[data-operation-id="{moved["operationId"]}"]')
        moved_row.hover()
        assert (
            page.locator(
                f'[data-operation-id="{moved["operationId"]}"].counterpart-highlight'
            ).count()
            == 2
        )
        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == len(plan["operations"])
        assert page.locator(".hierarchy-group-header .path-crumb").count() > 0


@pytest.mark.parametrize("number", ["03", "04"])
def test_held_out_plans_obey_general_hierarchy_invariants(page, number: str) -> None:
    plan = regression_plan(number)
    with running_visualizer(plan_dict=plan) as server:
        page.set_viewport_size({"width": 1692, "height": 1100})
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator(".unknown-context, .hierarchy-metadata-warning").count() == 0
        assert page.locator(".hierarchy-pane-before .context-title").count() > 0
        assert page.locator(".hierarchy-pane-after .context-title").count() > 0

        if number == "03":
            create_count = sum(operation["action"] == "create" for operation in plan["operations"])
            assert create_count > 0
            assert (
                page.locator(".hierarchy-pane-after .action-create.operation-row").count()
                == create_count
            )
        else:
            complete_operations = [
                operation for operation in plan["operations"] if operation["action"] == "complete"
            ]
            assert complete_operations
            assert page.locator(
                ".hierarchy-pane-before .action-complete.operation-row"
            ).count() == len(complete_operations)
            assert page.locator(".hierarchy-pane-after .terminal-complete").count() == len(
                complete_operations
            )
            operation_id = complete_operations[0]["operationId"]
            line = page.locator(
                f'.hierarchy-pane-after [data-operation-id="{operation_id}"] .task-title'
            ).evaluate("element => getComputedStyle(element).textDecorationLine")
            assert "line-through" in line

        operation_count = len(plan["operations"])
        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == operation_count
        assert page.locator(".hierarchy-group-header").count() > 0


def test_ordered_subtasks_and_loose_task_conversion_are_visually_traceable(page) -> None:
    plan = subtask_conversion_plan()
    long_source_title = (
        "Order food from the preferred restaurant after checking delivery timing and coupons"
    )
    plan["operations"][0]["after"]["subtasks"][0]["sourceTask"]["title"] = long_source_title
    plan["operations"][1]["target"]["title"] = long_source_title
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        before = page.locator('.hierarchy-pane-before [data-operation-id="build-dinner-checklist"]')
        after = page.locator('.hierarchy-pane-after [data-operation-id="build-dinner-checklist"]')
        assert before.locator(".subtask-title").all_inner_texts() == [
            "Pick up food",
            "Check coupons",
        ]
        assert after.locator(".subtask-title").all_inner_texts() == [
            "(1) Order food",
            "(2) Pick up food",
            "(3) Check the food is correct",
        ]
        assert after.locator(".subtask-row.done").count() == 1
        source = after.locator('[data-source-task-id="loose-order"]')
        assert source.locator(".subtask-source").inner_text() == "From source task"
        assert long_source_title in source.locator(".subtask-source").get_attribute("title")
        title_box = source.locator(".subtask-title").bounding_box()
        provenance_box = source.locator(".subtask-source").bounding_box()
        assert title_box is not None and title_box["width"] > 100
        assert provenance_box is not None and provenance_box["width"] <= 150
        assert source.locator(".subtask-loss").inner_text() == "Drops Estimated time duration"
        assert "Explicitly accepted source-task data loss" in source.locator(
            ".subtask-loss"
        ).get_attribute("title")
        source.hover()
        assert (
            page.locator('[data-operation-id="trash-loose-order"].counterpart-highlight').count()
            == 1
        )

        after.locator(".operation-details summary").click()
        details = after.locator(".operation-details[open]")
        assert details.locator(".subtask-diff-row").count() == 4
        detail_text = details.inner_text()
        assert f"Converted from loose task: {long_source_title}" in detail_text
        assert "Explicitly accepted loss: Estimated time duration" in detail_text
        assert "Removed" in detail_text
        assert "Completed" in detail_text
        assert "Moved from 1 to 2" in detail_text

        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == 2
        assert page.locator(".hierarchy-group-header .project-flag").count() == 1


def test_deep_hierarchy_scrolls_inside_each_pane_and_single_view_is_wider(page) -> None:
    with running_visualizer(plan_dict=hierarchy_plan(depth=12)) as server:
        page.set_viewport_size({"width": 1100, "height": 900})
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        before_scroll = page.locator(".hierarchy-pane-before .hierarchy-scroll")
        split_metrics = before_scroll.evaluate(
            "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth})"
        )
        assert split_metrics["scrollWidth"] > split_metrics["clientWidth"]
        before_scroll.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
        assert before_scroll.evaluate("element => element.scrollLeft") > 0
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

        page.get_by_role("radio", name="Now only").click()
        single_scroll = page.locator(".hierarchy-pane-before .hierarchy-scroll")
        single_metrics = single_scroll.evaluate(
            "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth})"
        )
        assert single_metrics["clientWidth"] > split_metrics["clientWidth"]
        assert page.locator(".context-title").count() == 12


def test_plan_recommended_day_section_visibility_is_not_a_global_preference(page) -> None:
    with running_visualizer(plan_dict=hierarchy_plan(show_day_sections=True)) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator("#day-sections-toggle").get_attribute("aria-pressed") == "true"
        assert page.locator(".day-section-title").all_inner_texts() == ["Waiting", "Main"]
        assert "daySection" not in " ".join(page.evaluate("Object.keys(window.localStorage)"))


def test_recurring_series_and_occurrence_scopes_are_unmistakable(page) -> None:
    path = [{"id": "category-sandbox", "type": "category", "title": "Sandbox"}]
    plan = {
        "schemaVersion": 1,
        "planId": "99999999-9999-4999-8999-999999999999",
        "createdAt": "2026-08-15T20:00:00-07:00",
        "summary": "Preview explicit recurrence scopes.",
        "operations": [
            {
                "operationId": "rename-series",
                "action": "update",
                "target": {
                    "type": "recurringTask",
                    "id": "series-fixture",
                    "title": "Review recurrence fixture",
                },
                "reason": "Clarify the future recurrence series.",
                "display": {"beforePath": path, "afterPath": path},
                "before": {
                    "title": "Review recurrence fixture",
                    "subtasks": [],
                    "cadence": {"type": "daily", "startDate": "2026-08-15"},
                },
                "after": {
                    "title": "Review updated recurrence fixture",
                    "subtasks": [
                        {"id": "fixture-step-a", "title": "First fixture step"},
                        {"id": "fixture-step-b", "title": "Second fixture step"},
                    ],
                    "cadence": {
                        "type": "n per week",
                        "startDate": "2026-08-15",
                        "weekdays": [2, 6],
                    },
                },
            },
            {
                "operationId": "trash-occurrence",
                "action": "trash",
                "target": {
                    "type": "task",
                    "id": "occurrence-fixture",
                    "title": "Review recurrence fixture",
                    "recurrence": {
                        "scope": "occurrence",
                        "seriesId": "series-fixture",
                        "seriesTitle": "Review recurrence fixture",
                        "scheduledDate": "2026-08-14",
                    },
                },
                "reason": "Remove only the stale generated occurrence.",
                "display": {"beforePath": path},
            },
        ],
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")

        assert page.locator(".scope-series").count() == 2
        assert page.locator(".scope-occurrence").count() == 1
        assert page.locator(".scope-series").all_inner_texts() == ["", ""]
        assert page.locator(".scope-occurrence").inner_text() == ""
        assert page.locator(".scope-series").first.get_attribute("aria-label") == (
            "Recurring task definition"
        )
        assert page.locator(".scope-occurrence").get_attribute("aria-label") == (
            "Recurring task occurrence scheduled for 2026-08-14"
        )
        occurrence_date = page.locator('[data-operation-id="trash-occurrence"] .occurrence-date')
        assert occurrence_date.inner_text() == "Scheduled 2026-08-14"
        assert occurrence_date.get_attribute("datetime") == "2026-08-14"
        assert page.get_by_text("THIS OCCURRENCE", exact=True).count() == 0
        assert page.get_by_text("ENTIRE SERIES", exact=True).count() == 0
        assert (
            page.locator(
                '.hierarchy-pane-after [data-operation-id="rename-series"] '
                ".hierarchy-icon-slot .object-icon"
            ).count()
            == 0
        )
        assert (
            page.locator(
                '.hierarchy-pane-after [data-operation-id="rename-series"] .recurrence-marker'
            ).count()
            == 1
        )
        assert page.locator(".hierarchy-pane-after .subtask-row").count() == 2
        assert (
            "n per week from 2026-08-15"
            in page.locator(
                '.hierarchy-pane-after [data-operation-id="rename-series"]'
            ).inner_text()
        )
        assert (
            page.locator(
                '.hierarchy-pane-after [data-operation-id="rename-series"] .task-title'
            ).bounding_box()["width"]
            >= 240
        )

        page.locator('[data-operation-id="trash-occurrence"] .operation-details summary').click()
        details = page.locator(
            '[data-operation-id="trash-occurrence"] .operation-details[open]'
        ).inner_text()
        assert "Recurrence scope: This occurrence only" in details
        assert "Occurrence date: 2026-08-14" in details

        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".diff-row").count() == 2
        assert page.locator(".scope-series").count() == 2
        assert page.locator(".scope-occurrence").count() == 1


def test_five_hundred_operation_plan_remains_reviewable(page) -> None:
    operations = [
        {
            "operationId": f"create-{index}",
            "action": "create",
            "target": {"type": "task", "id": str(UUID(int=index + 1))},
            "reason": "Browser scale coverage.",
            "display": {"afterSection": f"Section {index % 5}"},
            "after": {"title": f"Task {index}"},
        }
        for index in range(500)
    ]
    plan = {
        "schemaVersion": 1,
        "planId": "22222222-2222-4222-8222-222222222222",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "500-operation browser scale plan.",
        "operations": operations,
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator("#plan-view").wait_for(state="visible")
        assert page.locator(".operation-row").count() == 500
        assert page.locator(".hierarchy-context").count() == 5
        page.get_by_role("radio", name="Changes").click()
        assert page.locator(".section-group").count() == 5
        page.locator('[data-action-filter="update"]').click()
        assert page.locator("#empty-filter").is_visible()


def test_large_plan_mode_switch_paints_only_coherent_frames(page) -> None:
    operations = [
        {
            "operationId": f"create-{index}",
            "action": "create",
            "target": {"type": "task", "id": str(UUID(int=index + 1))},
            "reason": "Synthetic mode transition coverage.",
            "display": {"afterSection": f"Section {index % 8}"},
            "after": {"title": f"Task {index}"},
        }
        for index in range(500)
    ]
    plan = {
        "schemaVersion": 1,
        "planId": "22222222-2222-4222-8222-222222222222",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "Large synthetic mode-switch plan.",
        "operations": operations,
    }
    with running_visualizer(plan_dict=plan) as server:
        page.goto(server.url)
        page.locator(".hierarchy-preview").wait_for()
        page.locator(".hierarchy-branch .tree-toggle").first.click()
        assert (
            page.locator(".hierarchy-branch .tree-toggle").first.get_attribute("aria-expanded")
            == "false"
        )
        page.evaluate("window.scrollTo(0, 400)")
        scroll_before = page.evaluate("window.scrollY")
        frames = page.evaluate("""async () => {
          const samples = [];
          const sample = () => {
            const preview = document.querySelector('[data-mode-choice="preview"]');
            const changes = document.querySelector('[data-mode-choice="changes"]');
            const sections = document.querySelector('#sections');
            samples.push({
              mode: preview.getAttribute('aria-checked') === 'true' ? 'preview' : 'changes',
              previewTree: Boolean(sections.querySelector('.hierarchy-preview')),
              changesTree: Boolean(sections.querySelector('.split-diff')),
              dayControl: !document.querySelector('#day-sections-toggle').hidden,
              groupingControl: !document.querySelector('#changes-grouping-control').hidden,
              checked: changes.getAttribute('aria-checked') === 'true',
            });
          };
          for (const mode of ['changes', 'preview', 'changes', 'preview']) {
            document.querySelector(`[data-mode-choice="${mode}"]`).click();
            await new Promise(requestAnimationFrame);
            sample();
          }
          return samples;
        }""")
        assert [sample["mode"] for sample in frames] == ["changes", "preview", "changes", "preview"]
        assert all(
            sample["previewTree"] == (sample["mode"] == "preview")
            and sample["changesTree"] == (sample["mode"] == "changes")
            and sample["dayControl"] == (sample["mode"] == "preview")
            and sample["groupingControl"] == (sample["mode"] == "changes")
            and sample["checked"] == (sample["mode"] == "changes")
            for sample in frames
        )
        assert abs(page.evaluate("window.scrollY") - scroll_before) < 20
        assert (
            page.locator(".hierarchy-branch .tree-toggle").first.get_attribute("aria-expanded")
            == "false"
        )


def test_watched_file_refresh_preserves_mode_and_filters(page, tmp_path: Path) -> None:
    source = copy.deepcopy(EXAMPLE_PLAN)
    path = tmp_path / "review.json"
    raw = json.dumps(source).encode()
    path.write_bytes(raw)

    def reload_view() -> LoadedVisualization:
        updated = path.read_bytes()
        return LoadedVisualization(
            plan=parse_plan_bytes(updated), source_name=path.name, raw=updated
        )

    with running_visualizer(
        plan_dict=source,
        server_kwargs={
            "preloaded_raw": raw,
            "watch_paths": (path,),
            "reload_visualization": reload_view,
        },
    ) as server:
        page.goto(server.url)
        page.get_by_role("radio", name="Changes").click()
        page.locator('[data-action-filter="update"]').click()
        source["summary"] = "Updated synthetic review"
        path.write_bytes(json.dumps(source).encode())
        page.get_by_text("Updated synthetic review", exact=True).wait_for(timeout=10000)
        assert page.get_by_role("radio", name="Changes").get_attribute("aria-checked") == "true"
        assert page.locator('[data-action-filter="update"]').get_attribute("aria-pressed") == "true"
        assert page.locator(".split-diff").count() == 1


def test_watched_large_plan_preserves_collapsed_sections_and_scroll(page, tmp_path: Path) -> None:
    source = {
        "schemaVersion": 1,
        "planId": "22222222-2222-4222-8222-222222222222",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "Large watched synthetic review",
        "operations": [
            {
                "operationId": f"create-{index}",
                "action": "create",
                "target": {"type": "task", "id": str(UUID(int=index + 1))},
                "reason": "Synthetic reload coverage.",
                "display": {"afterSection": f"Section {index % 6}"},
                "after": {"title": f"Task {index}"},
            }
            for index in range(120)
        ],
    }
    path = tmp_path / "review.json"
    raw = json.dumps(source).encode()
    path.write_bytes(raw)

    def reload_view() -> LoadedVisualization:
        updated = path.read_bytes()
        return LoadedVisualization(
            plan=parse_plan_bytes(updated), source_name=path.name, raw=updated
        )

    with running_visualizer(
        plan_dict=source,
        server_kwargs={
            "preloaded_raw": raw,
            "watch_paths": (path,),
            "reload_visualization": reload_view,
        },
    ) as server:
        page.goto(server.url)
        toggle = page.locator(".hierarchy-branch .tree-toggle").first
        toggle.click()
        page.evaluate("window.scrollTo(0, 450)")
        before = page.evaluate("window.scrollY")
        source["summary"] = "Large watched synthetic review updated"
        path.write_bytes(json.dumps(source).encode())
        page.get_by_text("Large watched synthetic review updated", exact=True).wait_for(
            timeout=10000
        )
        assert (
            page.locator(".hierarchy-branch .tree-toggle").first.get_attribute("aria-expanded")
            == "false"
        )
        assert abs(page.evaluate("window.scrollY") - before) < 30


def test_browser_apply_reviews_exact_file_and_shows_receipt(page, tmp_path: Path) -> None:
    source = copy.deepcopy(EXAMPLE_PLAN)
    source["expectedAccount"] = {"userId": "123456", "email": "synthetic@example.com"}
    path = tmp_path / "review.json"
    raw = json.dumps(source).encode()
    path.write_bytes(raw)
    calls = []

    def preflight(plan):
        calls.append("preflight")
        return {
            "operations": len(plan.operations),
            "account": "synthetic@example.com",
            "warnings": [],
            "trash": 0,
        }

    def apply(_plan, _raw, assert_unchanged, reviewed_preflight):
        assert_unchanged()
        assert reviewed_preflight["account"] == "synthetic@example.com"
        calls.append("apply")
        return {"receipt_id": "synthetic-receipt", "receipt_path": "synthetic-receipt.json"}

    with running_visualizer(
        plan_dict=source,
        server_kwargs={
            "preloaded_raw": raw,
            "plan_path": path,
            "review_preflight": preflight,
            "review_apply": apply,
        },
    ) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.url)
        button = page.locator("#apply-reviewed")
        assert button.is_visible()
        assert page.locator("#plan-source-path").text_content() == str(path.resolve())
        assert "synthetic@example.com" in page.locator("#plan-account").text_content()
        button.click()
        page.get_by_text("Applied. Recovery receipt: synthetic-receipt.json").wait_for()
        assert page.locator("#review-status").inner_text() == "Applied plan — receipt verified"
        assert button.is_hidden()
        assert calls == ["preflight", "apply"]
