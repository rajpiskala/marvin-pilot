"use strict";

const THEME_KEY = "marvinPilot.visualizer.theme.v1";
const VIEW_KEY = "marvinPilot.visualizer.comparisonView.v1";
const THEMES = ["light", "dusk", "night"];
const VIEWS = ["split", "before", "after"];
const MODES = ["preview", "changes"];
const ACTIONS = ["create", "update", "complete", "trash"];
const CHANGE_GROUPINGS = ["after", "before", "plan", "day"];
const MAX_PLAN_BYTES = 4 * 1024 * 1024;
const COMPACT_FIELD_PREFIXES = {
  dueDate: "Due",
  startDate: "Starts",
  endDate: "Ends",
  plannedWeek: "Week",
  plannedMonth: "Month",
};

function readPreference(key, allowed, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value) ? value : fallback;
  } catch (_error) {
    return fallback;
  }
}

function savePreference(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_error) {
    // Preferences are optional; plan data is never written to web storage.
  }
}

let selectedTheme = readPreference(THEME_KEY, THEMES, "light");
let selectedView = readPreference(VIEW_KEY, VIEWS, "split");
let selectedMode = "preview";
let showDaySections = false;
let selectedChangeGrouping = "after";
let movedOnly = false;
let searchQuery = "";
let selectedOperationId = null;
let currentPlan = null;
const visibleActions = new Set(ACTIONS);

document.documentElement.dataset.theme = selectedTheme;

const elements = {
  landing: document.querySelector("#landing"),
  planView: document.querySelector("#plan-view"),
  fileInput: document.querySelector("#plan-file"),
  choosePlan: document.querySelector("#choose-plan"),
  openAnother: document.querySelector("#open-another"),
  dropZone: document.querySelector("#drop-zone"),
  errorPanel: document.querySelector("#error-panel"),
  errorMessage: document.querySelector("#error-message"),
  dismissError: document.querySelector("#dismiss-error"),
  summary: document.querySelector("#plan-summary"),
  fileName: document.querySelector("#plan-file-name"),
  total: document.querySelector("#plan-total"),
  planId: document.querySelector("#plan-id"),
  digest: document.querySelector("#plan-digest"),
  createCount: document.querySelector("#create-count"),
  updateCount: document.querySelector("#update-count"),
  completeCount: document.querySelector("#complete-count"),
  trashCount: document.querySelector("#trash-count"),
  daySectionsToggle: document.querySelector("#day-sections-toggle"),
  changesGroupingControl: document.querySelector("#changes-grouping-control"),
  changesGrouping: document.querySelector("#changes-grouping"),
  changesSearchControl: document.querySelector("#changes-search-control"),
  changesSearch: document.querySelector("#changes-search"),
  movedFilter: document.querySelector("#moved-filter"),
  movedCount: document.querySelector("#moved-count"),
  comparisonTray: document.querySelector("#comparison-tray"),
  sections: document.querySelector("#sections"),
  emptyFilter: document.querySelector("#empty-filter"),
};

function setTheme(theme, persist = true) {
  selectedTheme = THEMES.includes(theme) ? theme : "light";
  document.documentElement.dataset.theme = selectedTheme;
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.themeChoice === selectedTheme));
  });
  if (persist) {
    savePreference(THEME_KEY, selectedTheme);
  }
}

function setView(view, persist = true) {
  selectedView = VIEWS.includes(view) ? view : "split";
  document.querySelectorAll("[data-view-choice]").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.viewChoice === selectedView));
  });
  if (persist) {
    savePreference(VIEW_KEY, selectedView);
  }
  if (currentPlan) {
    renderSections();
  }
}

function setMode(mode) {
  selectedMode = MODES.includes(mode) ? mode : "preview";
  document.querySelectorAll("[data-mode-choice]").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.modeChoice === selectedMode));
  });
  elements.daySectionsToggle.hidden = selectedMode !== "preview";
  elements.changesGroupingControl.hidden = selectedMode !== "changes";
  elements.changesSearchControl.hidden = selectedMode !== "changes";
  if (currentPlan) {
    renderSections();
  }
}

function operationSearchText(operation) {
  return [
    operation.target_title,
    operation.before?.title,
    operation.after?.title,
    operation.reason,
    ...operation.before_path.map((entry) => entry.title),
    ...operation.after_path.map((entry) => entry.title),
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

function operationMatchesFilters(operation) {
  if (!visibleActions.has(operation.action)) {
    return false;
  }
  if (movedOnly && !operation.change_kinds.includes("moved")) {
    return false;
  }
  return selectedMode !== "changes" || !searchQuery || operationSearchText(operation).includes(searchQuery);
}

function syncDaySectionToggle() {
  elements.daySectionsToggle.setAttribute("aria-pressed", String(showDaySections));
  elements.daySectionsToggle.textContent = `Day sections: ${showDaySections ? "Shown" : "Hidden"}`;
}

function wireRadioGroup(selector, values, select) {
  const buttons = [...document.querySelectorAll(selector)];
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => select(button.dataset[values]));
    button.addEventListener("keydown", (event) => {
      let targetIndex = null;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        targetIndex = (index + 1) % buttons.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        targetIndex = (index - 1 + buttons.length) % buttons.length;
      } else if (event.key === "Home") {
        targetIndex = 0;
      } else if (event.key === "End") {
        targetIndex = buttons.length - 1;
      }
      if (targetIndex !== null) {
        event.preventDefault();
        buttons[targetIndex].click();
        buttons[targetIndex].focus();
      }
    });
  });
}

function showError(message) {
  elements.errorMessage.textContent = message;
  elements.errorPanel.hidden = false;
  elements.errorPanel.tabIndex = -1;
  elements.errorPanel.focus();
}

function clearError() {
  elements.errorPanel.hidden = true;
  elements.errorMessage.textContent = "";
}

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) {
    result.className = className;
  }
  if (text !== undefined && text !== null) {
    result.textContent = text;
  }
  return result;
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(tag, attributes = {}) {
  const result = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => result.setAttribute(name, value));
  return result;
}

function lineIcon(className, label) {
  const svg = svgElement("svg", {
    class: `object-icon ${className}`,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    role: "img",
    "aria-label": label,
  });
  return svg;
}

function objectIcon(type) {
  if (type === "task" || type === "subtask") {
    const task = node("span", `object-icon task-circle${type === "subtask" ? " subtask-circle" : ""}`);
    task.setAttribute("role", "img");
    task.setAttribute("aria-label", type === "subtask" ? "Subtask" : "Task");
    return task;
  }
  if (type === "project") {
    // Marvin's flag geometry, vendored as local static SVG paths.
    const flag = lineIcon("project-flag", "Project");
    flag.append(
      svgElement("path", {
        d: "M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z",
      }),
      svgElement("line", { x1: "4", y1: "22", x2: "4", y2: "15" }),
    );
    return flag;
  }
  if (type === "category") {
    // Marvin's folder geometry, vendored as a local static SVG path.
    const folder = lineIcon("category-folder", "Category");
    folder.append(
      svgElement("path", {
        d: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
      }),
    );
    return folder;
  }
  if (type === "inbox") {
    const inbox = lineIcon("inbox-tray", "Inbox");
    inbox.append(
      svgElement("polyline", { points: "22 12 16 12 14 15 10 15 8 12 2 12" }),
      svgElement("path", {
        d: "M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z",
      }),
    );
    return inbox;
  }
  const unknown = node("span", "object-icon unknown-context", "?");
  unknown.setAttribute("role", "img");
  unknown.setAttribute("aria-label", "Location metadata incomplete");
  return unknown;
}

function appendTaskTitle(container, title) {
  const match = title.match(/^((?:[01]?\d|2[0-3])(?::[0-5]\d)?\s?(?:am|pm)?)(\s+)(.+)$/i);
  if (!match) {
    container.textContent = title;
    return;
  }
  container.append(node("span", "leading-time", match[1]));
  container.append(document.createTextNode(match[2] + match[3]));
}

function paletteClass(value) {
  let hash = 0;
  for (const character of value) {
    hash = (hash * 31 + character.codePointAt(0)) >>> 0;
  }
  return `palette-${hash % 6}`;
}

function renderCard(
  card,
  sideName,
  targetType = "task",
  action = null,
  { showObjectIcon = true, changeKinds = [], badgeTitle = "" } = {},
) {
  const article = node("article", `task-card item-type-${targetType}`);
  if (!showObjectIcon) {
    article.classList.add("hierarchy-card");
  }
  article.setAttribute("aria-description", card.sparse_label);
  const taskLine = node("div", "task-line");
  if (showObjectIcon) {
    taskLine.append(objectIcon(targetType));
  }
  const title = node("div", "task-title");
  appendTaskTitle(title, card.title);
  taskLine.append(title);
  if (action) {
    const badgeText = (changeKinds.length > 0 ? changeKinds : [action]).join(" + ");
    const badge = node("span", `action-badge ${action}`, badgeText);
    if (badgeTitle) {
      badge.title = badgeTitle;
    }
    taskLine.append(badge);
  }
  article.append(taskLine);

  if (card.items.length > 0 || card.note_state === "clear") {
    const items = node("div", "task-items");
    card.items.forEach((item) => {
      const isTag = ["parent", "labels"].includes(item.kind);
      const itemNode = node(
        "span",
        `task-item ${item.kind}${isTag ? ` ${paletteClass(item.exact)}` : ""}${item.cleared ? " cleared" : ""}`,
      );
      itemNode.title = `${item.label}: ${item.exact}`;
      if (isTag && !item.cleared) {
        itemNode.append(node("span", "tag-prefix", "#"));
      } else if (COMPACT_FIELD_PREFIXES[item.field] && !item.cleared) {
        itemNode.append(
          node("span", "item-label", COMPACT_FIELD_PREFIXES[item.field]),
        );
      } else if (item.kind !== "estimate" && item.field !== "scheduledDate") {
        itemNode.append(node("span", "item-label", `${item.label}:`));
      }
      const itemText = item.cleared
        ? sideName === "before"
          ? "None"
          : "Clear"
        : item.text;
      const shorthand = item.field === "scheduledDate"
        ? "+"
        : item.kind === "estimate"
          ? "~"
          : "";
      itemNode.append(document.createTextNode(`${shorthand}${itemText}`));
      items.append(itemNode);
    });
    if (card.note_state === "clear") {
      items.append(node("span", "task-item cleared", "Note: Cleared"));
    }
    article.append(items);
  }

  if (card.note_state === "value") {
    const noteDetails = node("details", "task-note");
    noteDetails.append(node("summary", "", "Note"));
    noteDetails.append(node("p", "", card.note));
    article.append(noteDetails);
  }
  if (card.subtasks.length > 0) {
    const subtaskDetails = node("details", "task-subtasks");
    subtaskDetails.open = true;
    subtaskDetails.append(
      node(
        "summary",
        "subtask-summary",
        `${card.subtasks.length} subtask${card.subtasks.length === 1 ? "" : "s"}`,
      ),
    );
    const list = node("ol", "subtask-list");
    card.subtasks.forEach((subtask) => {
      const item = node("li", `subtask-row${subtask.done ? " done" : ""}`);
      item.dataset.subtaskId = subtask.id;
      if (subtask.source_task_id) {
        item.dataset.sourceTaskId = subtask.source_task_id;
      }
      item.append(objectIcon("subtask"));
      const title = node("span", "subtask-title", subtask.title);
      item.append(title);
      if (subtask.source_task_title) {
        const source = node("span", "subtask-source", `From ${subtask.source_task_title}`);
        source.title = `Converted from loose task ${subtask.source_task_id}`;
        item.append(source);
      }
      list.append(item);
    });
    subtaskDetails.append(list);
    article.append(subtaskDetails);
  }
  return article;
}

function renderDiffValue(value) {
  const cell = node("td", `diff-state-${value.state}`);
  cell.append(node("span", "diff-summary", value.summary));
  if (value.exact) {
    cell.append(node("code", "diff-exact", value.exact));
  }
  return cell;
}

function describePath(path, state = "path") {
  if (state === "unknown") {
    return "Not supplied in plan";
  }
  if (state === "legacy") {
    return `${path.map((entry) => entry.title).join(" / ")} (inferred)`;
  }
  return path.length > 0 ? path.map((entry) => entry.title).join(" / ") : "Root";
}

function describeDaySection(placement) {
  if (placement.state === "value") {
    return placement.title;
  }
  if (placement.state === "none") {
    return "No day section";
  }
  return "Not supplied in plan";
}

function renderSubtaskDiff(operation) {
  const before = operation.before?.subtasks || [];
  const after = operation.after?.subtasks || [];
  if (before.length === 0 && after.length === 0) {
    return null;
  }
  const beforeById = new Map(before.map((item, index) => [item.id, { item, index }]));
  const afterById = new Map(after.map((item, index) => [item.id, { item, index }]));
  const ids = [...before.map((item) => item.id)];
  after.forEach((item) => {
    if (!beforeById.has(item.id)) {
      ids.push(item.id);
    }
  });
  const section = node("section", "subtask-diff");
  section.append(node("h4", "", "Subtask changes"));
  const list = node("ul", "subtask-diff-list");
  ids.forEach((id) => {
    const oldEntry = beforeById.get(id);
    const newEntry = afterById.get(id);
    let kind = "unchanged";
    let description = "Unchanged";
    let title = oldEntry?.item.title || newEntry?.item.title || id;
    if (!oldEntry) {
      kind = "added";
      description = newEntry.item.source_task_title
        ? `Converted from loose task: ${newEntry.item.source_task_title}`
        : "Added";
    } else if (!newEntry) {
      kind = "removed";
      description = "Removed";
    } else {
      const changes = [];
      if (oldEntry.item.title !== newEntry.item.title) {
        changes.push(`Renamed from “${oldEntry.item.title}”`);
        title = newEntry.item.title;
      }
      if (oldEntry.item.done !== newEntry.item.done) {
        changes.push(newEntry.item.done ? "Completed" : "Reopened");
      }
      if (oldEntry.index !== newEntry.index) {
        changes.push(`Moved from ${oldEntry.index + 1} to ${newEntry.index + 1}`);
      }
      if (changes.length > 0) {
        kind = "changed";
        description = changes.join(" · ");
      }
    }
    if (kind === "unchanged") {
      return;
    }
    const row = node("li", `subtask-diff-row ${kind}`);
    row.append(
      node("span", "subtask-diff-kind", kind),
      node("strong", "", title),
      node("span", "subtask-diff-description", description),
    );
    list.append(row);
  });
  if (list.children.length === 0) {
    return null;
  }
  section.append(list);
  return section;
}

function renderOperationDetails(operation) {
  const details = node("details", "operation-details");
  const summary = node("summary");
  summary.setAttribute("aria-label", `Change details for ${operation.target_title}`);
  summary.append(node("span", "details-glyph", "•••"));
  details.append(summary);
  const body = node("div", "detail-body");
  const reason = node("p");
  reason.append(node("strong", "", "Reason: "));
  reason.append(document.createTextNode(operation.reason));
  body.append(reason);

  const identity = node("p");
  identity.append(node("strong", "", "Operation: "));
  identity.append(
    document.createTextNode(
      `#${operation.original_index} ${operation.action.toUpperCase()} · ${operation.operation_id}`,
    ),
  );
  identity.append(document.createElement("br"));
  identity.append(node("strong", "", `${operation.target_type[0].toUpperCase()}${operation.target_type.slice(1)} ID: `));
  identity.append(document.createTextNode(operation.target_id));
  if (operation.depends_on_operations.length > 0) {
    identity.append(document.createTextNode(" · Depends on operations: "));
    identity.append(document.createTextNode(operation.depends_on_operations.join(", ")));
  }
  body.append(identity);

  const locations = node("dl", "location-details");
  [
    [
      "Now location",
      operation.before
        ? describePath(operation.before_path, operation.before_path_state)
        : "No before state",
    ],
    [
      "After location",
      operation.after
        ? describePath(operation.after_path, operation.after_path_state)
        : "No active after state",
    ],
    [
      "Now day section",
      operation.before ? describeDaySection(operation.before_day_section) : "No before state",
    ],
    [
      "After day section",
      operation.after
        ? describeDaySection(operation.after_day_section)
        : "No active after state",
    ],
  ].forEach(([label, value]) => {
    const row = node("div");
    row.append(node("dt", "", label), node("dd", "", value));
    locations.append(row);
  });
  body.append(locations);

  if (operation.warnings.length > 0) {
    const warnings = node("ul", "warning-list");
    operation.warnings.forEach((warning) => warnings.append(node("li", "", warning)));
    body.append(warnings);
  }

  const subtaskDiff = renderSubtaskDiff(operation);
  if (subtaskDiff) {
    body.append(subtaskDiff);
  }

  const visibleDiffs = operation.diffs.filter((diff) => diff.field !== "subtasks");
  if (visibleDiffs.length > 0) {
    const wrap = node("div", "diff-wrap");
    const table = node("table", "diff-table");
    const head = node("thead");
    const headerRow = node("tr");
    ["Field", "Before", "After"].forEach((label) => headerRow.append(node("th", "", label)));
    head.append(headerRow);
    table.append(head);
    const tableBody = node("tbody");
    visibleDiffs.forEach((diff) => {
      const row = node("tr");
      const field = node("td");
      field.append(node("span", "diff-summary", diff.label));
      field.append(node("code", "diff-exact", diff.field));
      row.append(field, renderDiffValue(diff.before), renderDiffValue(diff.after));
      tableBody.append(row);
    });
    table.append(tableBody);
    wrap.append(table);
    body.append(wrap);
  }
  details.append(body);
  return details;
}

function renderStateOperation(operation, sideName) {
  const card = operation[sideName];
  if (!card) {
    return null;
  }
  const wrapper = node("article", `operation-row action-${operation.action}`);
  wrapper.dataset.operationId = operation.operation_id;
  wrapper.dataset.side = sideName;
  wrapper.tabIndex = 0;
  wrapper.setAttribute(
    "aria-label",
    `${operation.action} operation ${operation.operation_id}`,
  );
  wrapper.append(
    renderLocationBreadcrumb(operation, sideName),
    renderCard(card, sideName, operation.target_type, operation.action, {
      changeKinds: operation.change_kinds,
    }),
    renderOperationDetails(operation),
  );
  return wrapper;
}

function renderDiffCell(operation, sideName) {
  const cell = node("div", `diff-cell diff-cell-${sideName}`);
  const card = operation[sideName];
  if (card) {
    cell.append(
      renderLocationBreadcrumb(operation, sideName),
      renderCard(card, sideName, operation.target_type, operation.action, {
        changeKinds: operation.change_kinds,
      }),
    );
  } else {
    cell.classList.add("diff-cell-empty");
    cell.append(
      node(
        "span",
        "visually-hidden",
        sideName === "before"
          ? operation.before_empty_label
          : operation.after_empty_label,
      ),
    );
  }
  return cell;
}

function pathIdentity(path, state) {
  if (state === "root") {
    return "root";
  }
  return `${state}:${path.map((entry) => `${entry.type}:${entry.id}`).join("/")}`;
}

function operationGrouping(operation) {
  if (selectedChangeGrouping === "plan") {
    return { key: "plan", kind: "plan", title: "Plan order", path: [], state: "root" };
  }

  let sideName = selectedChangeGrouping === "before" ? "before" : "after";
  if (selectedChangeGrouping === "day") {
    sideName = selectedView === "before" ? "before" : "after";
  }
  if (!operation[sideName]) {
    const title = sideName === "before" ? "Created by this plan" : "Marvin Trash";
    return {
      key: `terminal:${sideName}`,
      kind: "terminal",
      title,
      path: [],
      state: "root",
    };
  }

  if (selectedChangeGrouping === "day") {
    const placement = operation[`${sideName}_day_section`];
    if (placement.state === "value") {
      return {
        key: `day:${placement.key}`,
        kind: "day",
        title: placement.title,
        path: [],
        state: "value",
      };
    }
    const title = placement.state === "none" ? "No Today section" : "Today section not supplied";
    return {
      key: `day:${placement.state}`,
      kind: "day",
      title,
      path: [],
      state: placement.state,
    };
  }

  const path = operation[`${sideName}_path`];
  const state = operation[`${sideName}_path_state`];
  return {
    key: `${sideName}:${pathIdentity(path, state)}`,
    kind: "location",
    title: state === "unknown" ? "Location not supplied" : state === "root" ? "Marvin root" : "",
    path,
    state,
  };
}

function groupedOperations(operations) {
  const groups = new Map();
  currentPlan.operations.forEach((operation) => {
    if (!operationMatchesFilters(operation)) {
      return;
    }
    const descriptor = operationGrouping(operation);
    if (!groups.has(descriptor.key)) {
      groups.set(descriptor.key, { ...descriptor, operations: [] });
    }
    groups.get(descriptor.key).operations.push(operation);
  });
  return [...groups.values()];
}

function renderPathCrumbs(path, state, { compact = false } = {}) {
  const crumbs = node("span", `path-crumbs${compact ? " compact" : ""}`);
  if (state === "unknown") {
    crumbs.append(node("span", "path-missing", "Location not supplied"));
    return crumbs;
  }
  if (state === "root" || path.length === 0) {
    crumbs.append(node("span", "path-root", "Marvin root"));
    return crumbs;
  }
  path.forEach((entry, index) => {
    const crumb = node("span", `path-crumb item-type-${entry.type}`);
    if (entry.color) {
      crumb.style.setProperty("--node-color", entry.color);
    }
    crumb.append(objectIcon(entry.type));
    if (entry.emoji) {
      crumb.append(node("span", "path-emoji", entry.emoji));
    }
    crumb.append(node("span", "path-title", entry.title));
    crumbs.append(crumb);
    if (index < path.length - 1) {
      crumbs.append(node("span", "path-separator", "›"));
    }
  });
  if (state === "legacy") {
    crumbs.append(node("span", "path-state-label", "Inferred location"));
  }
  return crumbs;
}

function renderLocationBreadcrumb(operation, sideName) {
  const location = node("div", "card-location");
  location.append(
    node("span", "card-location-label", sideName === "before" ? "Now" : "After"),
    renderPathCrumbs(
      operation[`${sideName}_path`],
      operation[`${sideName}_path_state`],
      { compact: true },
    ),
  );
  return location;
}

function renderChangeGroupHeader(group) {
  const header = node("header", "section-header diff-section-header hierarchy-group-header");
  const label = selectedChangeGrouping === "day"
    ? "Today section"
    : selectedChangeGrouping === "before"
      ? "Now location"
      : selectedChangeGrouping === "after"
        ? "After location"
        : "Review sequence";
  header.append(node("span", "group-kind-label", label));
  const title = node("h3", "hierarchy-group-title");
  if (group.kind === "location" && group.path.length > 0) {
    title.append(renderPathCrumbs(group.path, group.state));
  } else {
    title.append(node("span", "group-terminal-title", group.title));
  }
  title.append(
    node(
      "span",
      "section-counts",
      `${group.operations.length} change${group.operations.length === 1 ? "" : "s"}`,
    ),
  );
  header.append(title);
  return header;
}

function renderSplitPreview(operations) {
  const preview = node("section", "split-diff");
  const headings = node("header", "diff-pane-headings");
  headings.append(node("h2", "", "Now"), node("h2", "", "After (preview)"));
  preview.append(headings);

  let renderedCount = 0;
  groupedOperations(operations).forEach((section) => {
    const members = section.operations;
    if (members.length === 0) {
      return;
    }
    renderedCount += members.length;
    const group = node("section", "section-group diff-section");
    group.dataset.sectionKey = section.key;
    group.append(renderChangeGroupHeader(section));
    members.forEach((operation) => {
      const row = node("div", `operation-row diff-row action-${operation.action}`);
      row.dataset.operationId = operation.operation_id;
      row.tabIndex = 0;
      row.setAttribute(
        "aria-label",
        `${operation.action} operation ${operation.operation_id}`,
      );
      row.append(
        renderDiffCell(operation, "before"),
        renderDiffCell(operation, "after"),
        renderOperationDetails(operation),
      );
      group.append(row);
    });
    preview.append(group);
  });

  return { preview, renderedCount };
}

function renderStatePane(operations, sideName) {
  const pane = node("section", `preview-pane preview-pane-${sideName}`);
  const paneHeader = node("header", "preview-pane-header");
  paneHeader.append(
    node("h2", "", sideName === "before" ? "Now" : "After (preview)"),
  );
  pane.append(paneHeader);

  let renderedCount = 0;
  groupedOperations(operations).forEach((section) => {
    const members = section.operations.filter((operation) => operation[sideName]);
    if (members.length === 0) {
      return;
    }
    renderedCount += members.length;
    const group = node("section", "section-group");
    group.dataset.sectionKey = section.key;
    group.append(renderChangeGroupHeader({ ...section, operations: members }));
    members.forEach((operation) => group.append(renderStateOperation(operation, sideName)));
    pane.append(group);
  });

  if (renderedCount === 0) {
    pane.append(node("p", "pane-empty", "No visible item changes in this state."));
  }
  return { pane, renderedCount };
}

function hierarchyNodeIsVisible(item, operations) {
  const operation = item.operation_id ? operations.get(item.operation_id) : null;
  const ownVisible = operation && operationMatchesFilters(operation);
  return ownVisible || item.children.some((child) => hierarchyNodeIsVisible(child, operations));
}

function setHierarchyNodeExpanded(nodeKey, expanded) {
  document.querySelectorAll(".hierarchy-branch").forEach((branch) => {
    if (branch.dataset.nodeKey !== nodeKey) {
      return;
    }
    const row = branch.firstElementChild;
    const toggle = row?.querySelector(":scope > .tree-toggle");
    const children = branch.querySelector(":scope > .hierarchy-children");
    if (!toggle || !children) {
      return;
    }
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${branch.dataset.nodeTitle}`);
    children.hidden = !expanded;
  });
}

function renderHierarchyNode(item, sideName, operations, depth = 0) {
  const operation = item.operation_id ? operations.get(item.operation_id) : null;
  const ownVisible = operation && operationMatchesFilters(operation);
  const visibleChildren = item.children.filter((child) => hierarchyNodeIsVisible(child, operations));
  if (!ownVisible && visibleChildren.length === 0) {
    return null;
  }

  const branch = node("div", "hierarchy-branch");
  branch.dataset.nodeKey = item.key;
  branch.dataset.nodeTitle = item.title;
  branch.style.setProperty("--depth", String(depth));
  if (item.color) {
    branch.style.setProperty("--node-color", item.color);
  }

  const row = node(
    "div",
    `hierarchy-node-row ${ownVisible ? `operation-row action-${operation.action}` : "context-node"}`,
  );
  if (ownVisible) {
    row.dataset.operationId = operation.operation_id;
    row.dataset.side = sideName;
    row.tabIndex = 0;
    row.setAttribute("aria-label", `${operation.action} ${operation.target_type} ${item.title}`);
  }

  let childContainer = null;
  if (visibleChildren.length > 0) {
    const toggle = node("button", "tree-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", `Collapse ${item.title}`);
    row.append(toggle);
    childContainer = node("div", "hierarchy-children");
    const toggleExpanded = () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      setHierarchyNodeExpanded(item.key, !expanded);
    };
    toggle.addEventListener("click", toggleExpanded);
    branch._toggleExpanded = toggleExpanded;
  } else {
    row.append(node("span", "tree-toggle-spacer"));
  }

  const iconSlot = node("span", "hierarchy-icon-slot");
  iconSlot.append(objectIcon(item.type));
  row.append(iconSlot);

  if (ownVisible) {
    const content = node("div", "hierarchy-operation-content");
    let badgeTitle = "";
    if (operation.change_kinds.includes("moved")) {
      const otherSide = sideName === "before" ? "after" : "before";
      badgeTitle = sideName === "before"
        ? `Moves to ${describePath(operation[`${otherSide}_path`], operation[`${otherSide}_path_state`])}`
        : `Moved from ${describePath(operation[`${otherSide}_path`], operation[`${otherSide}_path_state`])}`;
    }
    content.append(
      renderCard(item.card, sideName, operation.target_type, operation.action, {
        showObjectIcon: false,
        changeKinds: operation.change_kinds,
        badgeTitle,
      }),
      renderOperationDetails(operation),
    );
    if (operation.action === "complete" && sideName === "after") {
      content.classList.add("terminal-complete");
    }
    row.append(content);
  } else {
    const context = node(
      visibleChildren.length > 0 ? "button" : "div",
      `hierarchy-context item-type-${item.type}`,
    );
    if (visibleChildren.length > 0) {
      context.type = "button";
      context.setAttribute("aria-label", `Toggle ${item.title}`);
      context.addEventListener("click", () => branch._toggleExpanded());
    }
    if (item.emoji) {
      context.append(node("span", "context-emoji", item.emoji));
    }
    context.append(node("span", "context-title", item.title));
    if (item.type === "unknown") {
      const label = item.id === "marvin-pilot:location-not-supplied"
        ? "Metadata missing"
        : "Inferred location";
      context.append(node("span", "context-label", label));
    }
    row.append(context);
  }
  branch.append(row);

  if (childContainer) {
    visibleChildren.forEach((child) => {
      const rendered = renderHierarchyNode(child, sideName, operations, depth + 1);
      if (rendered) {
        childContainer.append(rendered);
      }
    });
    branch.append(childContainer);
  }
  return branch;
}

function appendHierarchyRoots(container, roots, sideName, operations) {
  roots.forEach((root) => {
    const rendered = renderHierarchyNode(root, sideName, operations);
    if (rendered) {
      container.append(rendered);
    }
  });
}

function renderDaySectionGroup(group, sideName, operations) {
  const visibleOperationIds = group.operation_ids.filter((operationId) => {
    const operation = operations.get(operationId);
    return operation && operationMatchesFilters(operation) && operation[sideName];
  });
  if (visibleOperationIds.length === 0) {
    return null;
  }
  const details = node("details", `day-section day-section-${group.context_state}`);
  details.open = true;
  const summary = node("summary", "day-section-heading");
  summary.append(
    node("span", "day-section-title", group.title),
    node(
      "span",
      "day-section-count",
      `${visibleOperationIds.length} change${visibleOperationIds.length === 1 ? "" : "s"}`,
    ),
  );
  details.append(summary);
  const body = node("div", "day-section-body");
  appendHierarchyRoots(body, group.roots, sideName, operations);
  details.append(body);
  return details;
}

function renderHierarchyPane(operations, sideName) {
  const pane = node("section", `hierarchy-pane hierarchy-pane-${sideName}`);
  const paneHeader = node("header", "preview-pane-header");
  paneHeader.append(node("h2", "", sideName === "before" ? "Now" : "After (preview)"));
  pane.append(paneHeader);

  const scroll = node("div", "hierarchy-scroll");
  const tree = node("div", "hierarchy-tree");
  const state = currentPlan.previews[sideName];
  if (state.incomplete_operation_ids.length > 0) {
    const warning = node("div", "hierarchy-metadata-warning");
    warning.append(
      node("strong", "", "Hierarchy metadata incomplete. "),
      document.createTextNode(
        `${state.incomplete_operation_ids.length} change${state.incomplete_operation_ids.length === 1 ? "" : "s"} placed under “Location not supplied”.`,
      ),
    );
    tree.append(warning);
  }
  if (showDaySections) {
    state.day_sections.forEach((group) => {
      const rendered = renderDaySectionGroup(group, sideName, operations);
      if (rendered) {
        tree.append(rendered);
      }
    });
  } else {
    appendHierarchyRoots(tree, state.roots, sideName, operations);
  }
  scroll.append(tree);
  pane.append(scroll);

  const renderedCount = pane.querySelectorAll(".operation-row").length;
  if (renderedCount === 0) {
    scroll.replaceChildren(node("p", "pane-empty", "No visible item changes in this state."));
  }
  return { pane, renderedCount };
}

function renderHierarchyPreview(operations) {
  const preview = node("section", `hierarchy-preview hierarchy-preview-${selectedView}`);
  let renderedCount = 0;
  const sides = selectedView === "split" ? ["before", "after"] : [selectedView];
  sides.forEach((sideName) => {
    const result = renderHierarchyPane(operations, sideName);
    renderedCount += result.renderedCount;
    preview.append(result.pane);
  });
  return { preview, renderedCount };
}

function comparisonQueue(selectedOperation) {
  const matching = currentPlan.operations.filter((operation) => operationMatchesFilters(operation));
  if (selectedOperation?.change_kinds.includes("moved")) {
    return matching.filter((operation) => operation.change_kinds.includes("moved"));
  }
  return matching;
}

function renderComparisonSide(operation, sideName) {
  const side = node("div", `comparison-side comparison-side-${sideName}`);
  side.append(
    node("span", "comparison-side-label", sideName === "before" ? "Now" : "After (preview)"),
    renderPathCrumbs(
      operation[`${sideName}_path`],
      operation[`${sideName}_path_state`],
      { compact: true },
    ),
  );
  const card = operation[sideName];
  if (card) {
    side.append(
      renderCard(card, sideName, operation.target_type, operation.action, {
        changeKinds: operation.change_kinds,
      }),
    );
  } else {
    side.append(
      node(
        "div",
        "comparison-empty",
        sideName === "before" ? operation.before_empty_label : operation.after_empty_label,
      ),
    );
  }
  return side;
}

function operationElement(operationId, sideName) {
  const candidates = [...elements.sections.querySelectorAll("[data-operation-id]")].filter(
    (element) => element.dataset.operationId === operationId,
  );
  const exact = candidates.find((element) => element.dataset.side === sideName);
  return exact || (selectedMode === "changes" ? candidates[0] : null) || null;
}

function updateJumpDirections() {
  elements.comparisonTray.querySelectorAll("[data-jump-side]").forEach((button) => {
    const sideName = button.dataset.jumpSide;
    const operation = currentPlan?.operations.find(
      (candidate) => candidate.operation_id === selectedOperationId,
    );
    if (!operation?.[sideName]) {
      button.disabled = true;
      button.textContent = `No ${sideName === "before" ? "Now" : "After"} state`;
      return;
    }
    button.disabled = false;
    const target = operationElement(selectedOperationId, sideName);
    let direction = "";
    if (target) {
      const box = target.getBoundingClientRect();
      if (box.bottom < 0) {
        direction = " ↑";
      } else if (box.top > window.innerHeight) {
        direction = " ↓";
      } else {
        direction = " · visible";
      }
    }
    button.textContent = `Jump to ${sideName === "before" ? "Now" : "After"}${direction}`;
  });
}

function jumpToOperation(sideName) {
  if (selectedMode !== "preview") {
    setMode("preview");
  }
  if (selectedView !== "split" && selectedView !== sideName) {
    setView(sideName, false);
  }
  window.requestAnimationFrame(() => {
    const target = operationElement(selectedOperationId, sideName);
    if (!target) {
      return;
    }
    target.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    target.focus({ preventScroll: true });
    window.setTimeout(updateJumpDirections, 350);
  });
}

function cycleSelectedOperation(offset) {
  const selected = currentPlan.operations.find(
    (operation) => operation.operation_id === selectedOperationId,
  );
  const queue = comparisonQueue(selected);
  if (queue.length === 0) {
    return;
  }
  const index = Math.max(
    0,
    queue.findIndex((operation) => operation.operation_id === selectedOperationId),
  );
  setSelectedOperation(queue[(index + offset + queue.length) % queue.length].operation_id);
}

function renderComparisonTray() {
  const operation = currentPlan?.operations.find(
    (candidate) => candidate.operation_id === selectedOperationId,
  );
  if (!operation || !operationMatchesFilters(operation)) {
    selectedOperationId = null;
    elements.comparisonTray.hidden = true;
    elements.comparisonTray.replaceChildren();
    return;
  }

  const queue = comparisonQueue(operation);
  const queueIndex = queue.findIndex((candidate) => candidate.operation_id === operation.operation_id);
  const header = node("header", "comparison-tray-header");
  const heading = node("div");
  heading.append(
    node("span", "comparison-tray-eyebrow", "Pinned comparison"),
    node(
      "strong",
      "comparison-tray-title",
      `${queueIndex + 1} of ${queue.length}${operation.change_kinds.includes("moved") ? " moved items" : " visible changes"}`,
    ),
  );
  const navigation = node("div", "comparison-navigation");
  const previous = node("button", "secondary-button", "Previous");
  previous.type = "button";
  previous.disabled = queue.length <= 1;
  previous.addEventListener("click", () => cycleSelectedOperation(-1));
  const next = node("button", "secondary-button", "Next");
  next.type = "button";
  next.disabled = queue.length <= 1;
  next.addEventListener("click", () => cycleSelectedOperation(1));
  const close = node("button", "comparison-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close pinned comparison");
  close.addEventListener("click", () => setSelectedOperation(null));
  navigation.append(previous, next, close);
  header.append(heading, navigation);

  const sides = node("div", "comparison-sides");
  sides.append(renderComparisonSide(operation, "before"), renderComparisonSide(operation, "after"));
  const actions = node("div", "comparison-jumps");
  ["before", "after"].forEach((sideName) => {
    const button = node("button", "secondary-button");
    button.type = "button";
    button.dataset.jumpSide = sideName;
    button.addEventListener("click", () => jumpToOperation(sideName));
    actions.append(button);
  });

  elements.comparisonTray.replaceChildren(header, sides, actions);
  elements.comparisonTray.hidden = false;
  window.requestAnimationFrame(updateJumpDirections);
}

function setSelectedOperation(operationId) {
  selectedOperationId = operationId;
  document.querySelectorAll("[data-operation-id]").forEach((element) => {
    element.classList.toggle("selected-operation", element.dataset.operationId === operationId);
  });
  renderComparisonTray();
}

function renderSections() {
  const operations = new Map(currentPlan.operations.map((operation) => [operation.operation_id, operation]));
  const fragment = document.createDocumentFragment();
  let renderedCount = 0;

  if (selectedMode === "preview") {
    const result = renderHierarchyPreview(operations);
    renderedCount = result.renderedCount;
    fragment.append(result.preview);
  } else if (selectedView === "split") {
    const result = renderSplitPreview(operations);
    renderedCount = result.renderedCount;
    fragment.append(result.preview);
  } else {
    const result = renderStatePane(operations, selectedView);
    renderedCount = result.renderedCount;
    fragment.append(result.pane);
  }

  elements.sections.classList.toggle(
    "split-preview",
    selectedMode === "changes" && selectedView === "split",
  );
  elements.sections.classList.toggle(
    "single-preview",
    selectedMode === "changes" && selectedView !== "split",
  );
  elements.sections.classList.toggle("hierarchy-mode", selectedMode === "preview");
  elements.sections.replaceChildren(fragment);
  elements.emptyFilter.hidden = renderedCount !== 0;
  document.querySelectorAll("[data-operation-id]").forEach((element) => {
    element.classList.toggle(
      "selected-operation",
      Boolean(selectedOperationId) && element.dataset.operationId === selectedOperationId,
    );
  });
  renderComparisonTray();
}

function renderPlan(plan) {
  currentPlan = plan;
  movedOnly = false;
  searchQuery = "";
  selectedOperationId = null;
  elements.movedFilter.setAttribute("aria-pressed", "false");
  elements.changesSearch.value = "";
  showDaySections = Boolean(plan.previews?.show_day_sections_by_default);
  syncDaySectionToggle();
  elements.summary.textContent = plan.summary;
  elements.fileName.textContent = plan.source_name || "Browser upload";
  elements.total.textContent = `${plan.total_operations} operation${plan.total_operations === 1 ? "" : "s"}`;
  elements.planId.textContent = plan.plan_id;
  elements.digest.textContent = plan.digest;
  elements.createCount.textContent = plan.counts.create;
  elements.updateCount.textContent = plan.counts.update;
  elements.completeCount.textContent = plan.counts.complete;
  elements.trashCount.textContent = plan.counts.trash;
  const movedCount = plan.operations.filter((operation) => operation.change_kinds.includes("moved")).length;
  elements.movedCount.textContent = movedCount;
  elements.movedFilter.disabled = movedCount === 0;
  const hasDaySections = plan.operations.some(
    (operation) => operation.before_day_section.state === "value" || operation.after_day_section.state === "value",
  );
  const dayOption = elements.changesGrouping.querySelector('option[value="day"]');
  dayOption.disabled = !hasDaySections;
  dayOption.textContent = hasDaySections ? "Day section" : "Day section (not supplied)";
  if (!hasDaySections && selectedChangeGrouping === "day") {
    selectedChangeGrouping = "after";
    elements.changesGrouping.value = selectedChangeGrouping;
  }
  elements.landing.hidden = true;
  elements.planView.hidden = false;
  document.title = `Marvin Pilot - ${plan.summary}`;
  renderSections();
  window.scrollTo({ top: 0, behavior: "auto" });
}

async function responseJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    throw new Error("The local visualizer returned an unreadable response.");
  }
}

async function loadFile(file) {
  clearError();
  if (!file) {
    return;
  }
  if (file.size > MAX_PLAN_BYTES) {
    showError(`Plan exceeds the ${MAX_PLAN_BYTES}-byte limit.`);
    return;
  }
  const originalLabel = elements.choosePlan.textContent;
  elements.choosePlan.disabled = true;
  elements.choosePlan.textContent = "Validating…";
  try {
    const bytes = await file.arrayBuffer();
    const response = await fetch("api/plan", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Marvin-Pilot-Filename": encodeURIComponent(file.name),
      },
      body: bytes,
    });
    const result = await responseJson(response);
    if (!response.ok) {
      throw new Error(result.error?.message || `Plan validation failed (${response.status}).`);
    }
    renderPlan(result.plan);
  } catch (error) {
    showError(error instanceof Error ? error.message : "Could not load this plan.");
  } finally {
    elements.choosePlan.disabled = false;
    elements.choosePlan.textContent = originalLabel;
    elements.fileInput.value = "";
  }
}

async function loadCurrentPlan() {
  try {
    const response = await fetch("api/current", { headers: { Accept: "application/json" } });
    const result = await responseJson(response);
    if (!response.ok) {
      throw new Error(result.error?.message || "Could not load the preselected plan.");
    }
    if (result.plan) {
      renderPlan(result.plan);
    }
  } catch (error) {
    showError(error instanceof Error ? error.message : "The local visualizer stopped.");
  }
}

setTheme(selectedTheme, false);
setView(selectedView, false);
setMode(selectedMode);
wireRadioGroup("[data-theme-choice]", "themeChoice", setTheme);
wireRadioGroup("[data-view-choice]", "viewChoice", setView);
wireRadioGroup("[data-mode-choice]", "modeChoice", setMode);

elements.choosePlan.addEventListener("click", () => elements.fileInput.click());
elements.openAnother.addEventListener("click", () => elements.fileInput.click());
elements.fileInput.addEventListener("change", () => loadFile(elements.fileInput.files[0]));
elements.dismissError.addEventListener("click", clearError);
elements.daySectionsToggle.addEventListener("click", () => {
  showDaySections = !showDaySections;
  syncDaySectionToggle();
  if (currentPlan) {
    renderSections();
  }
});
elements.changesGrouping.addEventListener("change", () => {
  selectedChangeGrouping = CHANGE_GROUPINGS.includes(elements.changesGrouping.value)
    ? elements.changesGrouping.value
    : "after";
  if (currentPlan) {
    renderSections();
  }
});
elements.changesSearch.addEventListener("input", () => {
  searchQuery = elements.changesSearch.value.trim().toLocaleLowerCase();
  if (currentPlan) {
    renderSections();
  }
});
elements.movedFilter.addEventListener("click", () => {
  movedOnly = !movedOnly;
  elements.movedFilter.setAttribute("aria-pressed", String(movedOnly));
  if (currentPlan) {
    renderSections();
  }
});

["dragenter", "dragover"].forEach((eventName) => {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragging");
  });
});

elements.dropZone.addEventListener("drop", (event) => {
  const files = event.dataTransfer?.files;
  if (!files || files.length !== 1) {
    showError("Drop exactly one Marvin Pilot JSON plan.");
    return;
  }
  loadFile(files[0]);
});

const actionFilterButtons = [...document.querySelectorAll("[data-action-filter]")];

function syncActionFilterButtons() {
  actionFilterButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(visibleActions.has(button.dataset.actionFilter)));
  });
}

actionFilterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const action = button.dataset.actionFilter;
    const resetToAll = visibleActions.size === 1 && visibleActions.has(action);
    visibleActions.clear();
    if (resetToAll) {
      ACTIONS.forEach((value) => visibleActions.add(value));
    } else {
      visibleActions.add(action);
    }
    syncActionFilterButtons();
    if (currentPlan) {
      renderSections();
    }
  });
});

function setCounterpartHighlight(operationId, highlighted) {
  document.querySelectorAll("[data-operation-id]").forEach((element) => {
    if (element.dataset.operationId === operationId) {
      element.classList.toggle("counterpart-highlight", highlighted);
    }
  });
}

function setSourceTaskHighlight(sourceTaskId, highlighted) {
  const sourceOperation = currentPlan?.operations.find(
    (operation) => operation.target_id === sourceTaskId,
  );
  if (sourceOperation) {
    setCounterpartHighlight(sourceOperation.operation_id, highlighted);
  }
  document.querySelectorAll("[data-source-task-id]").forEach((element) => {
    if (element.dataset.sourceTaskId === sourceTaskId) {
      element.classList.toggle("source-highlight", highlighted);
    }
  });
}

elements.sections.addEventListener("pointerover", (event) => {
  const subtask = event.target.closest?.("[data-source-task-id]");
  if (subtask) {
    setSourceTaskHighlight(subtask.dataset.sourceTaskId, true);
  }
  const operation = event.target.closest?.("[data-operation-id]");
  if (operation) {
    setCounterpartHighlight(operation.dataset.operationId, true);
  }
});

elements.sections.addEventListener("pointerout", (event) => {
  const subtask = event.target.closest?.("[data-source-task-id]");
  if (subtask && !subtask.contains(event.relatedTarget)) {
    setSourceTaskHighlight(subtask.dataset.sourceTaskId, false);
  }
  const operation = event.target.closest?.("[data-operation-id]");
  if (operation && !operation.contains(event.relatedTarget)) {
    setCounterpartHighlight(operation.dataset.operationId, false);
  }
});

elements.sections.addEventListener("focusin", (event) => {
  const operation = event.target.closest?.("[data-operation-id]");
  if (operation) {
    setCounterpartHighlight(operation.dataset.operationId, true);
  }
});

elements.sections.addEventListener("focusout", (event) => {
  const operation = event.target.closest?.("[data-operation-id]");
  if (operation && !operation.contains(event.relatedTarget)) {
    setCounterpartHighlight(operation.dataset.operationId, false);
  }
});

elements.sections.addEventListener("click", (event) => {
  if (event.target.closest?.("button, summary, a, input, select")) {
    return;
  }
  const operation = event.target.closest?.("[data-operation-id]");
  if (operation) {
    setSelectedOperation(operation.dataset.operationId);
  }
});

elements.sections.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const operation = event.target.closest?.("[data-operation-id]");
  if (!operation || event.target.closest?.("button, summary, a, input, select")) {
    return;
  }
  event.preventDefault();
  setSelectedOperation(operation.dataset.operationId);
});

window.addEventListener("scroll", updateJumpDirections, { passive: true });
elements.sections.addEventListener("scroll", updateJumpDirections, { passive: true, capture: true });

loadCurrentPlan();
