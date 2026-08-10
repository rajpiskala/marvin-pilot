"use strict";

const THEME_KEY = "marvinPilot.visualizer.theme.v1";
const VIEW_KEY = "marvinPilot.visualizer.comparisonView.v1";
const THEMES = ["light", "dusk", "night"];
const VIEWS = ["split", "before", "after"];
const ACTIONS = ["create", "update", "trash"];
const MAX_PLAN_BYTES = 4 * 1024 * 1024;
const COMPACT_FIELD_PREFIXES = {
  scheduledDate: "On",
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
  trashCount: document.querySelector("#trash-count"),
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

function renderCard(card, sideName) {
  const article = node("article", "task-card");
  article.setAttribute("aria-description", card.sparse_label);
  const taskLine = node("div", "task-line");
  const checkbox = node("span", "checkbox-motif");
  checkbox.setAttribute("aria-hidden", "true");
  taskLine.append(checkbox);
  const title = node("div", "task-title");
  appendTaskTitle(title, card.title);
  taskLine.append(title);
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
      } else if (!["estimate"].includes(item.kind)) {
        itemNode.append(node("span", "item-label", `${item.label}:`));
      }
      const itemText = item.cleared
        ? sideName === "before"
          ? "None"
          : "Clear"
        : item.text;
      itemNode.append(document.createTextNode(itemText));
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
  identity.append(node("strong", "", "Task ID: "));
  identity.append(document.createTextNode(operation.target_id));
  if (operation.depends_on_operations.length > 0) {
    identity.append(document.createTextNode(" · Depends on operations: "));
    identity.append(document.createTextNode(operation.depends_on_operations.join(", ")));
  }
  body.append(identity);

  if (operation.warnings.length > 0) {
    const warnings = node("ul", "warning-list");
    operation.warnings.forEach((warning) => warnings.append(node("li", "", warning)));
    body.append(warnings);
  }

  const wrap = node("div", "diff-wrap");
  const table = node("table", "diff-table");
  const head = node("thead");
  const headerRow = node("tr");
  ["Field", "Before", "After"].forEach((label) => headerRow.append(node("th", "", label)));
  head.append(headerRow);
  table.append(head);
  const tableBody = node("tbody");
  operation.diffs.forEach((diff) => {
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
  wrapper.setAttribute(
    "aria-label",
    `${operation.action} operation ${operation.operation_id}`,
  );
  wrapper.append(renderCard(card, sideName), renderOperationDetails(operation));
  return wrapper;
}

function renderStatePane(operations, sideName) {
  const pane = node("section", `preview-pane preview-pane-${sideName}`);
  const paneHeader = node("header", "preview-pane-header");
  paneHeader.append(
    node("h2", "", sideName === "before" ? "Marvin now" : "Marvin after"),
  );
  pane.append(paneHeader);

  let renderedCount = 0;
  currentPlan.layouts[sideName].forEach((section) => {
    const members = section.operation_ids
      .map((operationId) => operations.get(operationId))
      .filter(
        (operation) =>
          operation && visibleActions.has(operation.action) && operation[sideName],
      );
    if (members.length === 0) {
      return;
    }
    renderedCount += members.length;
    const group = node("section", "section-group");
    group.dataset.sectionKey = section.key;
    const header = node("header", "section-header");
    header.append(node("h3", "", section.title));
    group.append(header);
    members.forEach((operation) => group.append(renderStateOperation(operation, sideName)));
    pane.append(group);
  });

  if (renderedCount === 0) {
    pane.append(node("p", "pane-empty", "No visible task changes in this state."));
  }
  return { pane, renderedCount };
}

function renderSections() {
  const operations = new Map(currentPlan.operations.map((operation) => [operation.operation_id, operation]));
  const fragment = document.createDocumentFragment();
  let renderedCount = 0;

  const sides = selectedView === "split" ? ["before", "after"] : [selectedView];
  sides.forEach((sideName) => {
    const result = renderStatePane(operations, sideName);
    renderedCount += result.renderedCount;
    fragment.append(result.pane);
  });

  elements.sections.classList.toggle("split-preview", selectedView === "split");
  elements.sections.classList.toggle("single-preview", selectedView !== "split");
  elements.sections.replaceChildren(fragment);
  elements.emptyFilter.hidden = renderedCount !== 0;
}

function renderPlan(plan) {
  currentPlan = plan;
  elements.summary.textContent = plan.summary;
  elements.fileName.textContent = plan.source_name || "Browser upload";
  elements.total.textContent = `${plan.total_operations} operation${plan.total_operations === 1 ? "" : "s"}`;
  elements.planId.textContent = plan.plan_id;
  elements.digest.textContent = plan.digest;
  elements.createCount.textContent = plan.counts.create;
  elements.updateCount.textContent = plan.counts.update;
  elements.trashCount.textContent = plan.counts.trash;
  elements.landing.hidden = true;
  elements.planView.hidden = false;
  document.title = `${plan.summary} — Marvin Pilot preview`;
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
wireRadioGroup("[data-theme-choice]", "themeChoice", setTheme);
wireRadioGroup("[data-view-choice]", "viewChoice", setView);

elements.choosePlan.addEventListener("click", () => elements.fileInput.click());
elements.openAnother.addEventListener("click", () => elements.fileInput.click());
elements.fileInput.addEventListener("change", () => loadFile(elements.fileInput.files[0]));
elements.dismissError.addEventListener("click", clearError);

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

loadCurrentPlan();
