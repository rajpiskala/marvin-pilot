"""Contrast-aware Rich terminal review for a live apply preflight."""

from __future__ import annotations

from collections import Counter

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from marvin_pilot.describe import FIELD_LABELS, format_plan_value
from marvin_pilot.models.plan_v1 import CompleteOperation, CreateOperation, UpdateOperation
from marvin_pilot.plan_io import plan_digest
from marvin_pilot.preflight import PreflightOperation, PreflightResult

ACTION_STYLES = {
    "create": "bold bright_green",
    "update": "bold bright_cyan",
    "complete": "bold bright_magenta",
    "trash": "bold bright_red",
}
TARGET_STYLES = {
    "task": "bold bright_blue",
    "project": "bold bright_magenta",
    "recurringTask": "bold bright_yellow",
}
TARGET_LABELS = {
    "task": "TASK",
    "project": "PROJECT",
    "recurringTask": "SERIES",
}
TARGET_EMOJIS = {
    "task": "📝",
    "project": "🚩",
    "recurringTask": "🔁",
    "occurrence": "🔂",
}
PATH_LABELS = {
    "inbox": "INBOX",
    "category": "CATEGORY",
    "project": "PROJECT",
    "task": "TASK",
}


def _supports_text(value: str, encoding: str) -> bool:
    try:
        value.encode(encoding, errors="strict")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_foreground(hex_color: str) -> tuple[str, float]:
    """Choose black or white text and return its WCAG contrast ratio."""

    luminance = _relative_luminance(hex_color)
    black_ratio = (luminance + 0.05) / 0.05
    white_ratio = 1.05 / (luminance + 0.05)
    return ("black", black_ratio) if black_ratio >= white_ratio else ("white", white_ratio)


def _color_chip(hex_color: str) -> Text:
    foreground, _ratio = contrast_foreground(hex_color)
    return Text(f" {hex_color} ", style=f"{foreground} on {hex_color}")


def _target_kind(checked: PreflightOperation) -> tuple[str, str]:
    operation = checked.operation
    if getattr(operation.target, "recurrence", None) is not None:
        return "OCCURRENCE", "occurrence"
    return TARGET_LABELS[operation.target.type], operation.target.type


def _target_title(checked: PreflightOperation) -> str:
    operation = checked.operation
    if isinstance(operation, CreateOperation):
        return operation.after.title
    return operation.target.title


def _target_heading(checked: PreflightOperation, encoding: str) -> Text:
    operation = checked.operation
    target_label, icon_key = _target_kind(checked)
    target_style = TARGET_STYLES.get(operation.target.type, "bold")
    heading = Text()
    heading.append(operation.action.upper(), style=ACTION_STYLES[operation.action])
    heading.append("  ")
    heading.append(f"[{target_label}]", style=target_style)

    live = checked.live_document or {}
    metadata_emoji = live.get("emoji")
    fallback_emoji = TARGET_EMOJIS[icon_key]
    emoji = metadata_emoji if isinstance(metadata_emoji, str) else fallback_emoji
    if _supports_text(emoji, encoding):
        heading.append(f"  {emoji}")
    heading.append(f"  {_target_title(checked)}", style="bold")

    metadata_color = live.get("color")
    if (
        isinstance(metadata_color, str)
        and len(metadata_color) == 7
        and metadata_color.startswith("#")
    ):
        try:
            int(metadata_color[1:], 16)
        except ValueError:
            pass
        else:
            heading.append("  ")
            heading.append_text(_color_chip(metadata_color.lower()))
    return heading


def _path_text(checked: PreflightOperation, side: str, encoding: str) -> Text:
    operation = checked.operation
    if isinstance(operation, CreateOperation) and side == "before":
        return Text("(not present)", style="dim")
    if operation.action == "trash" and side == "after":
        return Text("(removed by this plan)", style="bold bright_red")

    display = operation.display
    path = getattr(display, f"{side}Path") if display is not None else None
    section = getattr(display, f"{side}Section") if display is not None else None
    day_section = getattr(display, f"{side}DaySection") if display is not None else None
    rendered = Text()
    if day_section is not None:
        rendered.append(f"Today: {day_section.title} / ", style="bold")
    if path is not None:
        if not path:
            rendered.append("Marvin root", style="dim")
            return rendered
        for index, node in enumerate(path):
            if index:
                rendered.append(" / ", style="dim")
            if node.emoji is not None and _supports_text(node.emoji, encoding):
                rendered.append(f"{node.emoji} ")
            else:
                rendered.append(f"[{PATH_LABELS[node.type]}] ", style="dim")
            rendered.append(node.title)
            if node.color is not None:
                rendered.append(" ")
                rendered.append_text(_color_chip(node.color))
        return rendered
    if section is not None:
        rendered.append(section)
        rendered.append(" (section hint)", style="dim")
        return rendered
    rendered.append("Location not supplied", style="dim")
    return rendered


def _changes_table(checked: PreflightOperation) -> Table:
    operation = checked.operation
    table = Table(
        box=box.ASCII,
        show_edge=True,
        header_style="bold",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Field", style="bold", width=24, no_wrap=True)
    table.add_column("Now", ratio=2)
    table.add_column("After", ratio=2)

    if isinstance(operation, UpdateOperation):
        before = operation.before.model_dump(exclude_unset=True, mode="json")
        after = operation.after.model_dump(exclude_unset=True, mode="json")
        for field, new_value in after.items():
            table.add_row(
                FIELD_LABELS.get(field, field),
                format_plan_value(field, before[field]),
                format_plan_value(field, new_value),
            )
    elif isinstance(operation, CreateOperation):
        after = operation.after.model_dump(exclude_unset=True, mode="json")
        for field, new_value in after.items():
            table.add_row(
                FIELD_LABELS.get(field, field),
                "(not present)",
                format_plan_value(field, new_value),
            )
    elif isinstance(operation, CompleteOperation):
        table.add_row("completion", "open", f"completed at {operation.completedAt}")
        if operation.target.type == "task":
            if operation.completionDay is None:
                before = format_plan_value("scheduledDate", checked.live_document.get("day"))
                after = checked.compiled.desired_fields["day"]
                behavior = "derived from live state"
            else:
                before = operation.completionDay.before or "unassigned"
                after = operation.completionDay.after
                behavior = operation.completionDay.behavior
            table.add_row("completion history day", before, f"{after} ({behavior})")
    else:
        table.add_row(
            "document",
            "present",
            "deleted after full recovery snapshot is journaled",
        )
    return table


def _compiler_notes(checked: PreflightOperation) -> list[str]:
    operation = checked.operation
    desired = checked.compiled.desired_fields
    notes: list[str] = []
    if "firstScheduled" in desired:
        notes.append(f"firstScheduled={desired['firstScheduled']}")
    if operation.action == "create":
        notes.append(f"{operation.target.type} identity/defaults/timestamps")
    if operation.action == "complete":
        notes.append(
            "done state, historical completion timestamp, and dated completion-history bucket"
        )
    if operation.action == "trash":
        notes.append("API deletion after full recovery snapshot is journaled")
    return notes


def _operation_panel(
    checked: PreflightOperation,
    *,
    index: int,
    total: int,
    encoding: str,
) -> Panel:
    operation = checked.operation
    metadata = Table.grid(padding=(0, 2))
    metadata.add_column(style="bold", no_wrap=True)
    metadata.add_column()
    metadata.add_row("Operation ID", operation.operationId)
    metadata.add_row("Target ID", operation.target.id)
    if operation.target.type == "recurringTask":
        metadata.add_row("Scope", "entire recurring series")
    elif getattr(operation.target, "recurrence", None) is not None:
        recurrence = operation.target.recurrence
        metadata.add_row("Scope", "one generated occurrence")
        metadata.add_row("Series", f"{recurrence.seriesTitle} [{recurrence.seriesId}]")
        metadata.add_row("Occurrence date", recurrence.scheduledDate)
    metadata.add_row("Now location", _path_text(checked, "before", encoding))
    metadata.add_row("After location", _path_text(checked, "after", encoding))

    notes = _compiler_notes(checked)
    footer = Table.grid(padding=(0, 2))
    footer.add_column(style="bold", no_wrap=True)
    footer.add_column()
    footer.add_row("Reason", operation.reason)
    if operation.dependsOnOperations:
        footer.add_row("Depends on", ", ".join(operation.dependsOnOperations))
    if notes:
        footer.add_row("Pilot-managed", ", ".join(notes))

    return Panel(
        Group(_target_heading(checked, encoding), metadata, _changes_table(checked), footer),
        title=Text(f"{index}/{total}  {operation.operationId}", style="bold"),
        border_style=ACTION_STYLES[operation.action],
        box=box.ASCII,
        padding=(1, 2),
    )


def render_live_preflight_terminal(result: PreflightResult, *, encoding: str) -> Group:
    """Return a color-enhanced review whose meaning never depends on color alone."""

    counts = Counter(checked.operation.action for checked in result.operations)
    totals = ", ".join(
        f"{counts[action]} {action}"
        for action in ("create", "update", "complete", "trash")
        if counts[action]
    )
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold", no_wrap=True)
    summary.add_column()
    summary.add_row("Plan", result.plan.summary)
    summary.add_row("Plan ID", str(result.plan.planId))
    summary.add_row("Digest", plan_digest(result.plan))
    summary.add_row("Operations", f"{len(result.operations)} ({totals})")
    summary.add_row("Strict concurrency", "ON" if result.strict_concurrency else "OFF")
    has_warnings = bool(result.warnings)
    status_style = "bold bright_yellow" if has_warnings else "bold bright_green"
    status_text = (
        f"Live preflight: PASSED with {len(result.warnings)} warning(s)"
        if has_warnings
        else f"Live preflight: PASSED for {len(result.operations)} operation(s)"
    )
    header = Panel(
        Group(
            Text(status_text, style=status_style),
            summary,
        ),
        title=Text("REVIEWED APPLY PLAN", style="bold bright_green"),
        border_style="bright_yellow" if has_warnings else "bright_green",
        box=box.ASCII,
        padding=(1, 2),
    )
    warning_panel = None
    if has_warnings:
        warning_lines = Text()
        for index, warning in enumerate(result.warnings):
            if index:
                warning_lines.append("\n")
            warning_lines.append(
                f"{warning.operation_index}. [{warning.operation_id}] ", style="bold"
            )
            warning_lines.append(warning.message)
        warning_panel = Panel(
            warning_lines,
            title=Text("REVIEW WARNINGS", style="bold bright_yellow"),
            border_style="bright_yellow",
            box=box.ASCII,
            padding=(1, 2),
        )
    operations = [
        _operation_panel(
            checked,
            index=index,
            total=len(result.operations),
            encoding=encoding,
        )
        for index, checked in enumerate(result.operations, start=1)
    ]
    components = [header]
    if warning_panel is not None:
        components.append(warning_panel)
    components.extend(operations)
    return Group(*components, fit=False)
