// CV Tag Matcher prototype — vanilla JS, native HTML5 drag-and-drop, no
// build step and no dependencies (works opened directly via file://).
//
// Model: one board, `state.right`, an array of Super CV tag items grouped
// by `role`. The left column isn't a tag board at all -- it's a plain
// drop/paste zone for a job ad's raw text (wired in chat.js), which hands
// the pasted text to window.ChatAssistant to extract requirements and run a
// multiple-choice Q&A that updates the right board once answered.
//
// Every tag and group header is inline-editable via a double-click
// (see startInlineEdit / tagEl / groupHeaderEl).

const STORAGE_KEY = "cvExtTagBoard_v3"; // v3: single right board (left keyword board removed)

let state = null;
let currentDrag = null; // { ids: string[], isGroup?: true }

// ---------- undo/redo ----------
//
// A plain snapshot stack of the whole `state` object (right + roleMeta),
// not a per-mutation command log -- every kind of edit (add/remove/rename/
// level toggle/meta edit/drag reorder) already funnels through saveState()
// right after mutating `state` in place, so hooking history into saveState
// itself covers all of them uniformly with no per-call-site bookkeeping.
// In-memory only (not persisted to localStorage): a reload starts a fresh
// history anchored at whatever just loaded, same as a Reset does -- kept
// simple deliberately, since persisting a growing snapshot list would bloat
// localStorage for a feature whose whole point is transient edit history.
// Named editHistory/editHistoryIndex rather than plain `history` -- that
// bare identifier would shadow the browser's own window.history navigation
// object for every script on the page, a confusing footgun even though
// nothing here happens to call the real one today.
let editHistory = [];
let editHistoryIndex = -1;
const HISTORY_LIMIT = 200; // sane cap so a long editing session can't grow this unbounded

function snapshotState() {
  return JSON.parse(JSON.stringify(state));
}

// Starts (or restarts) history at the CURRENT state, e.g. right after a
// fresh load or a Reset -- that snapshot becomes the floor Undo can't go
// past, matching "undo goes back to the Reset default version."
function resetHistory() {
  editHistory = [snapshotState()];
  editHistoryIndex = 0;
  updateUndoRedoButtons();
}

// Records the state saveState() was just asked to persist. Any redo branch
// past the current point is dropped first -- editing after an undo is a new
// timeline, the same way every other undo/redo (text editors, Figma, ...)
// abandons whatever was undone rather than keeping it around as a fork.
function pushHistory() {
  editHistory = editHistory.slice(0, editHistoryIndex + 1);
  editHistory.push(snapshotState());
  if (editHistory.length > HISTORY_LIMIT) editHistory.shift();
  editHistoryIndex = editHistory.length - 1;
  updateUndoRedoButtons();
}

function updateUndoRedoButtons() {
  if (!els.undoBtn) return; // called once before init() wires els up
  els.undoBtn.disabled = editHistoryIndex <= 0;
  els.redoBtn.disabled = editHistoryIndex >= editHistory.length - 1;
}

// Restores a snapshot without recording a new history entry -- undo/redo
// move the existing pointer, they don't create fresh history of their own.
function restoreHistoryAt(index) {
  editHistoryIndex = index;
  state = JSON.parse(JSON.stringify(editHistory[editHistoryIndex]));
  persistToStorage();
  render();
  updateUndoRedoButtons();
}

function undo() {
  if (editHistoryIndex <= 0) return;
  restoreHistoryAt(editHistoryIndex - 1);
}

function redo() {
  if (editHistoryIndex >= editHistory.length - 1) return;
  restoreHistoryAt(editHistoryIndex + 1);
}

function uid(prefix) {
  return `${prefix}-${Math.random().toString(36).slice(2, 9)}`;
}

// The element actually positioned in a .group-tags list -- for most tags
// that's the .tag itself, but an Education tag sits one level deeper,
// nested inside its own .edu-row alongside its place/period pills (see
// eduRowEl). Every place that hides/moves/measures "the dragged tag" needs
// to operate on that whole row, not just the .tag hidden inside it, or the
// pills get left behind mid-drag instead of traveling with their tag.
function tagRowUnit(el) {
  return el.closest(".edu-row") || el;
}

// Deep-copies data.js's ROLE_META so state.roleMeta (the editable, persisted
// copy -- see groupHeaderEl/metaTagEl) starts from the same place/period
// values but never mutates the static source object.
function cloneRoleMeta() {
  const copy = {};
  for (const role in ROLE_META) {
    copy[role] = { ...ROLE_META[role] };
  }
  return copy;
}

function buildSeedState() {
  return {
    right: SUPER_CV_TAGS.map(({ role, text, level, place, period, section }) => ({
      id: uid("cv"),
      kind: "cv",
      role,
      text,
      ...(level ? { level } : {}), // only Interests tags carry a level (see tagEl)
      // Only Education tags carry their own place/period -- a job position's
      // instead lives role-wide in ROLE_META/state.roleMeta, since every tag
      // under that role is the same position (see eduRowEl vs. groupHeaderEl).
      ...(place ? { place } : {}),
      ...(period ? { period } : {}),
      // A job position's own "Tools & Skills" subsection tags (see
      // appendGroup) -- marks a tag as belonging to that pill-row instead of
      // the main bullet list, without splitting it into a separate `role`.
      ...(section ? { section } : {}),
      selected: false,
    })),
    roleMeta: cloneRoleMeta(),
  };
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return buildSeedState();
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.right)) {
      return buildSeedState();
    }
    // roleMeta was added after v3 was first shipped -- merge fresh defaults
    // underneath whatever a save already has, so older saves (and any role
    // ROLE_META gains later) pick up place/period without losing edits.
    parsed.roleMeta = { ...cloneRoleMeta(), ...(parsed.roleMeta || {}) };
    delete parsed.skillRoleMap; // retired along with Programming/Tools/Other Skills + Auto-match roles
    return parsed;
  } catch {
    return buildSeedState();
  }
}

function persistToStorage() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function saveState() {
  persistToStorage();
  pushHistory();
}

// Persists without recording a history entry -- used only for selecting a
// tag (see tagEl's click handler): which cards are highlighted is UI state
// worth remembering across a reload, but not a content edit anyone would
// want an Undo to step back through.
function saveSelectionOnly() {
  persistToStorage();
}

function resetState() {
  if (!confirm("Reset your Super CV to the original sample tags?")) return;
  localStorage.removeItem(STORAGE_KEY);
  state = buildSeedState();
  resetHistory();
  render();
}

// ---------- inline editing (tags + group headers) ----------

// Swaps `hostEl`'s content for a text input/textarea pre-filled with
// `currentValue`. `dragRootEl` (the enclosing .tag or .group-header) is
// temporarily set undraggable so clicking/selecting text inside the input
// doesn't fight the native HTML5 drag. Calls `finish(newValue)` on commit
// (Enter, or blur) — `newValue` may be `""` if the user cleared it — or
// `finish(null)` on cancel (Escape). The caller is responsible for mutating
// state + re-rendering either way, including deciding what an empty commit
// means (see finishCvTagEdit for tags: empty commit deletes the tag).
function startInlineEdit(dragRootEl, hostEl, currentValue, multiline, finish, placeholder) {
  dragRootEl.draggable = false;
  hostEl.innerHTML = "";

  const input = document.createElement(multiline ? "textarea" : "input");
  if (multiline) input.rows = 2;
  else input.type = "text";
  input.className = "inline-edit-input";
  input.value = currentValue;
  if (placeholder) input.placeholder = placeholder;

  let done = false;
  function settle(value) {
    if (done) return;
    done = true;
    finish(value);
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !(multiline && e.shiftKey)) {
      e.preventDefault();
      settle(input.value.trim());
    } else if (e.key === "Escape") {
      e.preventDefault();
      settle(null);
    }
  });
  input.addEventListener("blur", () => settle(input.value.trim()));
  // Don't let interacting with the input bubble into the tag's click-select,
  // its own dblclick-to-edit (which would otherwise wipe and recreate this
  // very input mid-edit instead of letting the browser's native
  // double-click-to-select-a-word behavior happen), or accidentally arm a drag.
  ["click", "mousedown", "dblclick", "dragstart"].forEach((evt) =>
    input.addEventListener(evt, (e) => e.stopPropagation())
  );

  hostEl.appendChild(input);
  input.focus();
  input.select();
}

// Shared commit logic for editing a CV tag's text, whether that's an
// existing tag (double-click, isNew=false) or a brand-new one just created
// by the "+" tile (isNew=true, no popup -- edit mode opens immediately so
// typing happens right in place, see addTagToRole). Committing empty text
// deletes the tag outright rather than leaving a blank line behind, for
// both cases alike. Cancelling (Escape) only deletes for a brand-new tag,
// which has no real prior content to revert to; an existing tag's Escape
// just keeps its old text, same as before.
function startEditingCvTag(el, body, item, isNew) {
  startInlineEdit(
    el,
    body,
    item.text,
    true,
    (val) => {
      if (val === null) {
        if (isNew) {
          state.right = state.right.filter((t) => t.id !== item.id);
          // Cancelling a brand-new tag nets back to exactly the pre-add
          // state (see addTagToRole, which doesn't record history for the
          // blank placeholder either) -- nothing real happened, so nothing
          // to undo; persist only.
          persistToStorage();
        }
        render();
        return;
      }
      if (!val) {
        state.right = state.right.filter((t) => t.id !== item.id);
        // Same "nothing real happened" reasoning for a brand-new tag
        // committed empty. Clearing an EXISTING tag's text, though, is a
        // real delete -- that one goes through saveState() so Undo can
        // bring it back.
        if (isNew) persistToStorage();
        else saveState();
        render();
        return;
      }
      item.text = val;
      saveState();
      render();
    },
    isNew ? "Type the new line, or leave blank to cancel…" : undefined
  );
}

function iconButtonEl(extraClass, title, glyph, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `icon-btn ${extraClass}`;
  btn.title = title;
  btn.textContent = glyph;
  btn.addEventListener("mousedown", (e) => e.stopPropagation());
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick();
  });
  return btn;
}

function deleteButtonEl(title, onClick) {
  return iconButtonEl("delete-btn", title, "×", onClick);
}

// A place/period "pill" -- editable like any CV tag (double-click, same
// startInlineEdit primitive) but never removable: unlike startEditingCvTag,
// an empty commit or Escape both just keep the existing value instead of
// deleting it, since there's no such thing as a job/degree with no place or
// period, only a corrected one. `getValue`/`setValue` decouple this from
// *where* the value actually lives -- a job position's is shared role-wide
// in state.roleMeta (see groupHeaderEl), while an education entry's is its
// own item's place/period field (see eduRowEl), since several degrees can
// share one "Education" role/header.
function metaTagEl(dragRootEl, getValue, setValue) {
  const el = document.createElement("span");
  el.className = "meta-tag";
  el.textContent = getValue();
  el.title = "Double-click to edit";
  // Same reasoning as iconButtonEl's mousedown guard: stop this from also
  // arming an ancestor drag (the group header, or this row's own tag) or
  // bubbling into the header's dblclick-to-rename-the-role handler.
  el.addEventListener("mousedown", (e) => e.stopPropagation());
  el.addEventListener("click", (e) => e.stopPropagation());
  el.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    startInlineEdit(dragRootEl, el, getValue(), false, (val) => {
      if (val) {
        setValue(val);
        saveState();
      }
      render();
    });
  });
  return el;
}

// Wraps action buttons (just delete, now that edit moved to double-click) in
// a single hover-revealed group (see .tag-actions / .group-header-actions in style.css).
function actionsEl(className, buttons) {
  const wrap = document.createElement("div");
  wrap.className = className;
  buttons.forEach((b) => wrap.appendChild(b));
  return wrap;
}

// ---------- rendering ----------

function tagEl(item, { showInlineRole = true } = {}) {
  const el = document.createElement("div");
  el.className = "tag tag-cv";
  if (item.selected) el.classList.add("selected");
  el.draggable = true;
  el.dataset.id = item.id;

  const body = document.createElement("div");
  body.className = "tag-body";

  if (showInlineRole) {
    const role = document.createElement("span");
    role.className = "tag-role";
    role.textContent = item.role;
    body.appendChild(role);
  }

  // Interests tags carry a strong/neutral level star.
  const hasLevel = item.role === "Interests";
  const textRow = hasLevel ? document.createElement("div") : body;
  if (hasLevel) {
    textRow.className = "level-row";
    body.appendChild(textRow);

    // Strong vs. neutral, click to toggle -- a filled star reads as
    // "actively pursue/strong in this", an outline star as "have it, don't
    // lead with it". Defaults to neutral for a tag with no level yet (e.g.
    // any other CV tag dragged into this group).
    const levelBtn = document.createElement("button");
    levelBtn.type = "button";
    levelBtn.className = "level-toggle" + (item.level === "strong" ? " strong" : "");
    levelBtn.textContent = item.level === "strong" ? "★" : "☆";
    levelBtn.title =
      item.level === "strong" ? "Strong interest — click to mark neutral" : "Neutral interest — click to mark strong";
    levelBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    levelBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      item.level = item.level === "strong" ? "neutral" : "strong";
      saveState();
      render();
    });
    textRow.appendChild(levelBtn);
  }

  const text = document.createElement("span");
  text.className = "tag-text";
  text.textContent = item.text;
  textRow.appendChild(text);
  el.appendChild(body);

  el.appendChild(
    actionsEl("tag-actions", [
      deleteButtonEl("Delete", () => {
        state.right = state.right.filter((t) => t.id !== item.id);
        saveState();
        render();
      }),
    ])
  );

  el.addEventListener("click", () => {
    item.selected = !item.selected;
    saveSelectionOnly();
    el.classList.toggle("selected", item.selected);
  });

  el.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    startEditingCvTag(el, body, item, false);
  });

  el.addEventListener("dragstart", (e) => {
    // Dragging a selected tag drags the whole current selection together;
    // dragging an unselected tag drags just that one.
    const ids = item.selected
      ? state.right.filter((t) => t.selected).map((t) => t.id)
      : [item.id];
    currentDrag = { ids };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", ids.join(",")); // Firefox needs data set
    // Deferred a frame so the browser finishes capturing its own drag-ghost
    // image from the (still fully visible) element before we hide it.
    requestAnimationFrame(() => {
      const dragEls = ids
        .map((id) => document.querySelector(`.tag[data-id="${id}"]`))
        .filter(Boolean)
        .map(tagRowUnit);
      currentDrag.previewRects = dragEls.map((t) => t.getBoundingClientRect());
      dragEls.forEach((t) => t.classList.add("dragging", "drag-ghost"));
    });
  });

  el.addEventListener("dragend", clearDragVisuals);

  return el;
}

function clearDragVisuals() {
  // Always fires on dragend regardless of how the drag ended (a successful
  // drop already called render() and made all of this moot, but a drop
  // outside any zone, or an Escape-cancelled drag, would otherwise leave a
  // hidden source element / stray placeholder / mid-transition transform
  // behind — this is the unconditional final cleanup for all of those).
  document.querySelectorAll(".tag.dragging, .group-header.dragging, .edu-row.dragging").forEach((t) => {
    t.classList.remove("dragging", "drag-ghost");
    t.style.transform = "";
    t.style.transition = "";
  });
  removeDropPlaceholder();
  currentDrag = null;
  document.querySelectorAll(".board-body").forEach((b) => {
    b.classList.remove("drag-over");
  });
}

// Inserts a new, empty tag into `role`'s run and opens inline-edit on it
// immediately -- no popup, the user just types directly where it lands
// (see startEditingCvTag, which deletes it again if left/committed empty).
// `afterId` given -> right after that tag (used by the "+" tile that sits
// last in a non-empty group, so the new tag becomes the new last one);
// omitted -> the group is currently empty (only the phantom Interests case
// today), so fall back to wherever PHANTOM_GROUP_AFTER anchors that role.
function addTagToRole(role, afterId, extra = {}) {
  let insertAt;
  if (afterId) {
    const idx = state.right.findIndex((t) => t.id === afterId);
    insertAt = idx === -1 ? state.right.length : idx + 1;
  } else {
    const afterRole = PHANTOM_GROUP_AFTER[role];
    const lastAnchorIdx = afterRole ? state.right.reduce((acc, t, idx) => (t.role === afterRole ? idx : acc), -1) : -1;
    insertAt = lastAnchorIdx !== -1 ? lastAnchorIdx + 1 : state.right.length;
  }
  const newTag = {
    id: uid("cv"),
    kind: "cv",
    role,
    text: "",
    selected: false,
    ...extra, // e.g. { section: "tools" } for a job position's Tools & Skills "+" tile
  };
  if (role === "Interests") newTag.level = "neutral";
  state.right.splice(insertAt, 0, newTag);
  // The blank placeholder isn't a committed edit yet -- the real history
  // entry comes from startEditingCvTag's commit right below, so Undo
  // removes the finished tag in one step instead of leaving an empty one
  // behind after only undoing the text.
  persistToStorage();
  render();

  const el = document.querySelector(`.tag[data-id="${newTag.id}"]`);
  const body = el && el.querySelector(".tag-body");
  if (el && body) startEditingCvTag(el, body, newTag, true);
}

// A "+" tile that sits last in a group's tag list (see renderGroupedList),
// styled to match a tag (a small pill in a wrapped group, a full-width
// dashed row otherwise) but deliberately NOT a .tag itself -- it must stay
// invisible to computeDropTarget's `.tag` scan and every other tag-specific
// selector (drag, hover-actions, FLIP tracking, ...), since it isn't a real
// CV line to reorder or drop onto.
function addTagButtonEl(role, afterId, extra) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "tag-add-btn";
  btn.title = extra && extra.section === "tools" ? `Add a tool/skill to ${role}` : `Add to ${role}`;
  btn.textContent = "+";
  btn.addEventListener("click", () => addTagToRole(role, afterId, extra));
  return btn;
}

// Every "add a new experience" click uses the same template role name, so a
// second/third one needs a distinguishing suffix or it would merge into the
// first experience's run instead of becoming its own group (grouping is
// purely by exact `role` string match -- see renderGroupedList). Numbered
// rather than uniqued invisibly, so it's honestly visible that this is a
// fresh placeholder waiting to be renamed via the header's own dblclick.
function nextExperienceRoleName() {
  const base = "<Company> · <Role>";
  if (!state.right.some((t) => t.role === base)) return base;
  let n = 2;
  while (state.right.some((t) => t.role === `${base} #${n}`)) n++;
  return `${base} #${n}`;
}

// Adds a brand-new experience section: a job-position role (with its own
// ROLE_META place/period, same as any real job would have -- see
// groupHeaderEl), one Accomplishment bullet, and one Tools & Skills tag, all
// template placeholders for the user to rename/edit in place. `afterId`
// anchors it right after a specific existing item (the last tag of the last
// experience's Tools & Skills subsection, from the "+ Add experience"
// button below it -- see renderGroupedList); null means there's no existing
// experience to anchor after, so it goes at the very start of the board,
// where experience sections conventionally begin.
function addExperience(afterId) {
  const role = nextExperienceRoleName();
  state.roleMeta[role] = { place: "<City, Country>", period: "<yyyy-mm> – <yyyy-mm>" };

  const newItems = [
    { id: uid("cv"), kind: "cv", role, text: "<Accomplishment — what you did and the impact it had>", selected: false },
    { id: uid("cv"), kind: "cv", role, section: "tools", text: "<tool>", selected: false },
  ];

  let insertAt;
  if (afterId) {
    const idx = state.right.findIndex((t) => t.id === afterId);
    insertAt = idx === -1 ? state.right.length : idx + 1;
  } else {
    insertAt = 0;
  }
  state.right.splice(insertAt, 0, ...newItems);
  saveState();
  render();
}

// Sits below the last experience's Tools & Skills (or, with no experience
// section at all yet, wherever renderGroupedList/render decide experiences
// should start) -- a full-width button distinct from a group's own small "+"
// tile (see addTagButtonEl), since this adds a whole new section rather than
// one tag.
function addExperienceButtonEl(afterId) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "add-experience-btn";
  btn.textContent = "+ Add experience";
  btn.title = "Add a new experience section — a company/role header, one accomplishment tag, and one tool tag";
  btn.addEventListener("click", () => addExperience(afterId));
  return btn;
}

// A group header is draggable too and carries the ids of the contiguous run
// of tags it's currently labeling, so dragging it moves that whole run
// together. An empty `runIds` means this is the phantom Interests header
// shown even with nothing in it yet (see renderGroupedList) -- nothing to
// rename, delete, or drag in that case (adding the first tag is the "+"
// tile in the body below, not here).
function groupHeaderEl(role, runIds) {
  const el = document.createElement("div");
  el.className = "group-header";
  el.draggable = runIds.length > 0;

  const text = document.createElement("span");
  text.className = "group-header-text";
  text.textContent = role;
  el.appendChild(text);

  // Place + time period, for job positions only (see data.js -> ROLE_META,
  // state.roleMeta) -- Education's own place/period sit per-entry instead
  // (see eduRowEl), since several degrees share one "Education" header.
  // Editable via double-click just like any CV tag (see metaTagEl) but not
  // draggable and not a .tag element, so still invisible to
  // computeDropTarget/every other tag-specific selector -- same reasoning
  // as the "+" tile (see addTagButtonEl). Unlike a CV tag, these are never
  // removable: there's no × button, and an empty commit or Escape both
  // just keep the old value.
  const meta = state.roleMeta ? state.roleMeta[role] : null;
  if (meta) {
    const metaWrap = document.createElement("span");
    metaWrap.className = "group-header-meta";
    [
      ["place", meta.place],
      ["period", meta.period],
    ].forEach(([field, value]) => {
      if (!value) return;
      metaWrap.appendChild(
        metaTagEl(
          el,
          () => state.roleMeta[role][field],
          (val) => { state.roleMeta[role][field] = val; }
        )
      );
    });
    el.appendChild(metaWrap);
  }

  if (runIds.length > 0) {
    el.appendChild(
      actionsEl("group-header-actions", [
        deleteButtonEl("Delete group", () => {
          const n = runIds.length;
          if (!confirm(`Delete this group and its ${n} tag${n === 1 ? "" : "s"}?`)) return;
          state.right = state.right.filter((t) => !runIds.includes(t.id));
          saveState();
          render();
        }),
      ])
    );
  }

  if (runIds.length === 0) {
    return el; // phantom empty-group header -- no rename/drag to wire up
  }

  el.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    startInlineEdit(el, el, role, false, (val) => {
      if (val) {
        state.right.forEach((t) => {
          if (runIds.includes(t.id)) t.role = val;
        });
        saveState();
      }
      render();
    });
  });

  el.addEventListener("dragstart", (e) => {
    currentDrag = { ids: [...runIds], isGroup: true };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", runIds.join(","));
    requestAnimationFrame(() => {
      const tagEls = runIds
        .map((id) => document.querySelector(`.tag[data-id="${id}"]`))
        .filter(Boolean)
        .map(tagRowUnit);
      // Header height + each tag's own height = the combined block size the
      // placeholder should represent once this whole group lands elsewhere.
      currentDrag.previewRects = [el.getBoundingClientRect(), ...tagEls.map((t) => t.getBoundingClientRect())];
      el.classList.add("dragging", "drag-ghost");
      tagEls.forEach((t) => t.classList.add("dragging", "drag-ghost"));
    });
  });
  el.addEventListener("dragend", clearDragVisuals);

  return el;
}

// Roles whose tags are short enough to sit several-to-a-line instead of one
// per line. Add a role name here to switch it to the compact wrap layout —
// nothing else needs to change (see .group-tags--wrap in style.css and the
// row-aware computeDropTarget below). A job position's own "Tools & Skills"
// subsection (see appendGroup) always gets this layout too, hardcoded there
// rather than listed here, since it's never the group's own top-level role.
const WRAP_GROUP_ROLES = new Set(["Interests"]);

// A role that should always have a header in the right board, even with
// zero tags in it yet, anchored right after the given previous role -- so
// there's always a "+" to add the first one instead of the section simply
// not existing until data.js or the chat assistant happens to create it.
const PHANTOM_GROUP_AFTER = { Interests: "Publications" };

// One line for one Education entry: its own draggable/editable/removable
// description tag, plus its place/period pills sitting beside it on the
// same line. Unlike a job position -- where every tag under a role is the
// same position, so place/period sit once on the shared group header (see
// groupHeaderEl) -- several degrees all share one "Education" role/header,
// so each entry's place/period has to live on the item itself instead (see
// data.js's Education tags) and render per-row here.
function eduRowEl(item) {
  const row = document.createElement("div");
  row.className = "edu-row";
  row.appendChild(tagEl(item, { showInlineRole: false }));
  if (item.place || item.period) {
    const metaWrap = document.createElement("span");
    metaWrap.className = "group-header-meta"; // same pill-row look as a job position's header meta
    if (item.place) {
      metaWrap.appendChild(
        metaTagEl(row, () => item.place, (val) => { item.place = val; })
      );
    }
    if (item.period) {
      metaWrap.appendChild(
        metaTagEl(row, () => item.period, (val) => { item.period = val; })
      );
    }
    row.appendChild(metaWrap);
  }
  return row;
}

// Renders items with one shared header per contiguous run of the same
// `role`, instead of a plain flat list. Reordering can split a role's tags
// into more than one run (e.g. a Skills tag dragged in between two
// Mpya tags) -- that's expected: each run still gets its own header, so
// the label directly above a tag is always accurate for what's below it,
// and editing/dragging a header only ever affects that specific run.
//
// Each run's tags live in their own .group-tags wrapper (not flat siblings
// of the header) so a role can independently choose a one-per-line column
// layout or a several-per-line wrapped layout. computeDropTarget() below
// already searches all .tag descendants rather than direct children, so
// this extra nesting doesn't affect drag-and-drop indexing.
function appendGroup(role, runIds, groupItems, listEl) {
  listEl.appendChild(groupHeaderEl(role, runIds));

  // Job positions (any role with a ROLE_META/state.roleMeta entry) get a
  // "Tools & Skills" subsection at the bottom of their own group -- each
  // tool/skill mentioned for that specific job, marked with
  // `section: "tools"` on the item rather than a distinct `role` (see
  // data.js), so it stays part of the SAME contiguous run as the bullets
  // above instead of splitting into its own top-level header.
  const isJobPosition = !!(state.roleMeta && state.roleMeta[role]);
  const mainItems = isJobPosition ? groupItems.filter((it) => it.section !== "tools") : groupItems;
  const toolItems = isJobPosition ? groupItems.filter((it) => it.section === "tools") : [];

  const tagsWrap = document.createElement("div");
  tagsWrap.className = "group-tags" + (WRAP_GROUP_ROLES.has(role) ? " group-tags--wrap" : "");
  tagsWrap.dataset.role = role; // read back on drop to merge a moved tag into this group
  mainItems.forEach((item) =>
    tagsWrap.appendChild(item.place || item.period ? eduRowEl(item) : tagEl(item, { showInlineRole: false }))
  );
  // The "+" tile sits last, after the actual last tag -- see addTagButtonEl
  // for why it deliberately isn't a .tag itself.
  tagsWrap.appendChild(addTagButtonEl(role, mainItems[mainItems.length - 1] ? mainItems[mainItems.length - 1].id : null));
  listEl.appendChild(tagsWrap);

  if (isJobPosition) {
    const subheading = document.createElement("div");
    subheading.className = "subsection-header";
    subheading.textContent = "Tools & Skills";
    listEl.appendChild(subheading);

    // Same wrapped pill-row layout as Interests (see WRAP_GROUP_ROLES) --
    // hardcoded here since this subsection is always wrap-style regardless
    // of whether the parent job-position `role` itself is in that set (it
    // never is).
    const toolsWrap = document.createElement("div");
    toolsWrap.className = "group-tags group-tags--wrap";
    toolsWrap.dataset.role = role;
    toolsWrap.dataset.section = "tools";
    toolItems.forEach((item) => toolsWrap.appendChild(tagEl(item, { showInlineRole: false })));
    const lastToolId = toolItems.length
      ? toolItems[toolItems.length - 1].id
      : mainItems[mainItems.length - 1] ? mainItems[mainItems.length - 1].id : null;
    toolsWrap.appendChild(addTagButtonEl(role, lastToolId, { section: "tools" }));
    listEl.appendChild(toolsWrap);
  }
}

function renderGroupedList(items, listEl) {

  const hasRole = (role) => items.some((it) => it.role === role);
  const isJobPositionRole = (role) => !!(state.roleMeta && state.roleMeta[role]);

  // No experience section anywhere yet -- offer "+ Add experience" right at
  // the top, where experience sections conventionally begin, rather than
  // only after a run that doesn't exist.
  if (!items.some((it) => isJobPositionRole(it.role))) {
    listEl.appendChild(addExperienceButtonEl(null));
  }

  let i = 0;
  while (i < items.length) {
    const role = items[i].role;
    let j = i;
    const runIds = [];
    while (j < items.length && items[j].role === role) {
      runIds.push(items[j].id);
      j++;
    }
    appendGroup(role, runIds, items.slice(i, j), listEl);

    // Right after the LAST experience group in a contiguous cluster of them
    // (the next run, if any, isn't itself a job position), offer a "+" to
    // add a whole new experience section -- see addExperience.
    const nextRole = j < items.length ? items[j].role : null;
    if (isJobPositionRole(role) && !(nextRole && isJobPositionRole(nextRole))) {
      listEl.appendChild(addExperienceButtonEl(runIds[runIds.length - 1]));
    }

    for (const [phantomRole, afterRole] of Object.entries(PHANTOM_GROUP_AFTER)) {
      if (role === afterRole && !hasRole(phantomRole)) appendGroup(phantomRole, [], [], listEl);
    }

    i = j;
  }
}

function render() {
  const items = state.right;
  els.rightList.innerHTML = "";
  if (items.length === 0) {
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent = "Drag a tag here";
    els.rightList.appendChild(hint);
    els.rightList.appendChild(addExperienceButtonEl(null));
  } else {
    renderGroupedList(items, els.rightList);
  }
  els.rightCount.textContent = `${items.length} lines`;
}

// ---------- drag/drop wiring on the right board ----------

// Row-aware: groups tags into visual rows (tags whose top edges line up
// within a few px), which is exactly one tag for a normal one-per-line
// group and several for a wrapped group (e.g. Skills — see
// .group-tags--wrap). A single-tag row falls back to the plain "above/below
// its own vertical midpoint" rule, unchanged from before wrapping existed.
// A multi-tag row instead picks a slot by horizontal position once the
// cursor is within that row's vertical band. Searches all .tag descendants
// (not just direct children) since tags now nest inside a .group-tags
// wrapper rather than sitting as flat siblings of listEl.
//
// Returns { index, container, localIndex }:
//   - `index` is a position in the FULL .tag list (unfiltered, dragged
//     items included) -- moveItems() adjusts against that same unfiltered
//     state array, so this must stay in that numbering. A previous version
//     of this function filtered excludeIds OUT before assigning indices,
//     which silently renumbered every tag after the dragged one and
//     produced indices moveItems() interpreted completely differently --
//     confirmed via a real mouse-driven drag trace: the placeholder stuck
//     at the wrong slot for most of a Skills-row drag, then jumped once,
//     and the drop landed one slot off from the drag_to target. Excluded
//     (hidden, currently-dragged) tags must be skipped while *building*
//     rows (so a display:none item's degenerate (0,0,0,0) rect can't be
//     mistaken for a real row) without ever shifting anyone else's index.
//   - `container`/`localIndex` are the exact DOM insertion point for the
//     live placeholder, derived directly from the matched element itself
//     rather than re-deriving "which tag sits at global index N"
//     afterward. That distinction matters right at a group boundary: "one
//     past the last tag of a wrapped row" and "the first tag of the next
//     (column) group" can be the very same global index, but they live in
//     two different .group-tags containers with different layouts --
//     deriving the container from the row actually matched (not a bare
//     number looked up later) is what keeps the placeholder from jumping
//     into the wrong one.
function computeDropTarget(listEl, clientX, clientY, excludeIds = []) {
  const allTags = [...listEl.querySelectorAll(".tag")];
  if (!allTags.length) {
    return { index: 0, container: listEl, localIndex: listEl.children.length };
  }

  // Route through tagRowUnit so an Education tag's placeholder lands
  // between rows (in the .group-tags list its .edu-row actually sits in),
  // not inside the row next to the pills.
  const insertBefore = (el, index) => {
    const unit = tagRowUnit(el);
    return {
      index,
      container: unit.parentElement,
      localIndex: [...unit.parentElement.children].indexOf(unit),
    };
  };
  const insertAfter = (el, index) => {
    const unit = tagRowUnit(el);
    return {
      index,
      container: unit.parentElement,
      localIndex: [...unit.parentElement.children].indexOf(unit) + 1,
    };
  };

  const rows = [];
  allTags.forEach((el, index) => {
    if (excludeIds.includes(el.dataset.id)) return;
    const rect = el.getBoundingClientRect();
    const prevRow = rows[rows.length - 1];
    if (prevRow && Math.abs(rect.top - prevRow.items[0].rect.top) < 3) {
      prevRow.items.push({ el, rect, index });
    } else {
      rows.push({ items: [{ el, rect, index }] });
    }
  });

  // An empty .group-tags container (e.g. Interests before anything's been
  // added to it -- see renderGroupedList) has no .tag descendants, so the
  // scan above never sees it at all -- without this, there'd be no way to
  // ever drop a tag INTO one; the drop would always resolve to whichever
  // real row happens to sit above/below it instead, no matter how carefully
  // the cursor was aimed at the empty zone shown on screen (confirmed via a
  // live-mouse trace: landing squarely inside the empty zone's own
  // now-non-zero-height box still resolved to the neighboring group).
  // Insert a synthetic single-item "row" for each one, positioned by its
  // own rect, so it competes in the exact same row-ordering and gap-aware
  // zone logic as every real row below -- matched, it resolves directly to
  // that container (nothing to call insertBefore/insertAfter on, since
  // there's no tag inside it yet).
  [...listEl.querySelectorAll(".group-tags")].forEach((groupEl) => {
    if (groupEl.querySelector(".tag")) return; // not empty
    const rect = groupEl.getBoundingClientRect();
    let insertionIndex = allTags.length;
    for (let node = groupEl.nextElementSibling; node; node = node.nextElementSibling) {
      const firstTagInside = node.matches(".tag") ? node : node.querySelector(".tag");
      if (firstTagInside) {
        const idx = allTags.indexOf(firstTagInside);
        if (idx !== -1) insertionIndex = idx;
        break;
      }
    }
    rows.push({ empty: true, container: groupEl, index: insertionIndex, items: [{ rect }] });
  });
  rows.sort((a, b) => a.items[0].rect.top - b.items[0].rect.top);

  if (!rows.length) {
    return { index: 0, container: listEl, localIndex: listEl.children.length };
  }

  // Every row "claims" the vertical span up to the midpoint between itself
  // and its next surviving neighbor, not just its own footprint. A row's
  // own edge is NOT a safe boundary: excluding the dragged tag(s) removes
  // their row from this list entirely, so a row sitting right next to the
  // dragged item (the last row of the group before it, or the first row of
  // the group after it) can suddenly face a much bigger-than-normal gap --
  // an entire missing row's worth of space, plus a wrapped row's own
  // trailing margin and/or the next group's header. A by-my-own-edge check
  // has no way to split that fairly, so a cursor anywhere in that whole gap
  // -- including squarely where the dragged tag used to sit -- was awarded
  // entirely to whichever row's check happened to run next, which silently
  // reassigned the drop to the wrong group. Computing each row's zone as
  // "up to the midpoint to the next row" keeps zones contiguous and fairly
  // split regardless of what got excluded.
  for (let r = 0; r < rows.length; r++) {
    const row = rows[r];
    const rowBottom = Math.max(...row.items.map((it) => it.rect.bottom));
    const nextRow = rows[r + 1];
    const zoneBottom = nextRow
      ? (rowBottom + Math.min(...nextRow.items.map((it) => it.rect.top))) / 2
      : Infinity;
    if (clientY >= zoneBottom) continue;

    if (row.empty) {
      // Not literally DOM-empty -- it still holds its own "+" add tile
      // (see addTagButtonEl), which must stay first. Appending (rather than
      // hardcoding 0) is what keeps the placeholder from landing before it.
      return { index: row.index, container: row.container, localIndex: row.container.children.length };
    }

    if (row.items.length === 1) {
      // The zone above only decided WHICH row claims this y -- within that
      // zone we still need before-this-tag vs. after-this-tag, using the
      // tag's own midpoint (not the zone's), so hovering over its bottom
      // half (or the gap right after it) still means "insert after," not
      // "insert before" for the whole zone.
      const { el, rect, index } = row.items[0];
      if (clientY < rect.top + rect.height / 2) return insertBefore(el, index);
      return insertAfter(el, index + 1);
    }
    for (const it of row.items) {
      if (clientX < it.rect.left + it.rect.width / 2) return insertBefore(it.el, it.index);
    }
    const last = row.items[row.items.length - 1];
    return insertAfter(last.el, last.index + 1);
  }
  const lastTag = allTags[allTags.length - 1];
  return insertAfter(lastTag, allTags.length);
}

// Moves the tags identified by `ids` to `dropIndex` within state.right
// (including moving a whole group's ids together), adjusting for the moved
// items' own removal shifting later
// positions.
function moveItems(ids, dropIndex) {
  const movingItems = state.right.filter((t) => ids.includes(t.id));
  if (!movingItems.length) return false;

  let removedBefore = 0;
  state.right.forEach((t, i) => {
    if (i < dropIndex && ids.includes(t.id)) removedBefore++;
  });
  const adjustedIndex = dropIndex - removedBefore;

  state.right = state.right.filter((t) => !ids.includes(t.id));
  const clampedIndex = Math.max(0, Math.min(adjustedIndex, state.right.length));
  state.right.splice(clampedIndex, 0, ...movingItems);
  return true;
}

// ---------- live drag preview (FLIP-animated reflow + placeholder frame) ----------
//
// Native HTML5 drag-and-drop gives us a cursor-following ghost image and
// nothing else — the underlying list never visually reflows until drop. To
// preview "here's where it'll land" before releasing the mouse, we:
//   1. hide the real dragged element(s) in place (see the drag-ghost class
//      added in tagEl/groupHeaderEl's dragstart, once the browser has
//      already captured its own drag-ghost image from the visible element),
//   2. insert an empty, glowing-bordered .drop-placeholder at the currently
//      computed drop index, sized to roughly match what's being dragged,
//   3. FLIP-animate every other tag/header from its pre-insertion position
//      to its new one, so they visibly slide out of the way.
// Moving the cursor to a new slot repeats this; moving it off any valid
// target reverts everything back to the original layout. None of this
// touches `state` — only a real drop does.
//
// IMPORTANT: dragover fires continuously (a real mouse generates many more
// ticks than a script's paced movement) at the SAME container the native
// drag session is tracking as the drop target. An earlier version mutated
// that container (remove + recreate the placeholder, reset every tracked
// element's transform) unconditionally on *every* tick, even when nothing
// had actually changed. That's exactly the kind of continuous DOM churn on
// the live drop-target element that's known to destabilize native HTML5
// drag-and-drop in real browsers -- reported symptom was "drop usually just
// doesn't do anything." Below, all of that work only runs when the resolved
// target has actually changed, and the placeholder node is reused (moved),
// never destroyed and recreated, so a real drag now touches the tracked
// DOM only a handful of times total instead of on every single tick.
let activePlaceholder = null;
let previewListEl = null;
let previewTargetIndex = null; // computeDropTarget().index for the current placeholder
let previewTargetContainer = null; // computeDropTarget().container for the current placeholder -- same
// index can validly mean two different containers right at a group boundary
// (see computeDropTarget's docs), so both must match to call it "unchanged."

// previewTargetIndex doubles as what drop() commits directly, instead of
// calling computeDropTarget a second time -- so the result can never
// diverge from what was just previewed (a second independent measurement
// could disagree with the first if a FLIP transition happened to still be
// mid-flight at the exact moment of drop).

function removeDropPlaceholder() {
  if (activePlaceholder && activePlaceholder.parentElement) {
    activePlaceholder.parentElement.removeChild(activePlaceholder);
  }
  activePlaceholder = null;
  previewListEl = null;
  previewTargetIndex = null;
  previewTargetContainer = null;
}

function flipRects(elements) {
  const map = new Map();
  elements.forEach((el) => map.set(el, el.getBoundingClientRect()));
  return map;
}

function playFlip(elements, before) {
  elements.forEach((el) => {
    const b = before.get(el);
    if (!b) return;
    const a = el.getBoundingClientRect();
    const dx = b.left - a.left;
    const dy = b.top - a.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
    el.style.transition = "none";
    el.style.transform = `translate(${dx}px, ${dy}px)`;
    el.getBoundingClientRect(); // force a reflow so the transition below is picked up
    requestAnimationFrame(() => {
      el.style.transition = "transform 180ms ease";
      el.style.transform = "";
    });
  });
}

// Tags/headers currently being dragged are excluded from the FLIP set (they're
// hidden, not sliding anywhere) via the shared "dragging" marker class.
// Routed through tagRowUnit so an Education tag's whole row (pills
// included) is what actually gets tracked/transformed, not just the .tag
// nested inside it -- otherwise the pills would visibly detach from their
// tag as everything else FLIPs out of the way.
function trackedPreviewEls(listEl) {
  const units = new Set([...listEl.querySelectorAll(".tag, .group-header")].map(tagRowUnit));
  return [...units].filter((el) => !el.classList.contains("dragging"));
}

function sizePlaceholder(placeholder, wrap) {
  const rects = currentDrag && currentDrag.previewRects;
  if (!rects || !rects.length) return;
  const gap = 8;
  if (wrap) {
    placeholder.style.width = `${rects.reduce((sum, r) => sum + r.width, 0) + gap * (rects.length - 1)}px`;
    placeholder.style.height = `${Math.max(...rects.map((r) => r.height))}px`;
  } else {
    placeholder.style.width = "";
    placeholder.style.height = `${rects.reduce((sum, r) => sum + r.height, 0) + gap * (rects.length - 1)}px`;
  }
}

function updateReorderPreview(listEl, clientX, clientY, excludeIds) {
  const { index, container, localIndex } = computeDropTarget(listEl, clientX, clientY, excludeIds);

  // Nothing to do if the cursor is still over the same slot it was on the
  // last tick -- this is the common case for most dragover ticks a real
  // mouse generates, and skipping it entirely is what keeps this from
  // constantly mutating the container the browser is tracking as the drop
  // target (see the file note above).
  if (activePlaceholder && previewTargetIndex === index && previewTargetContainer === container) {
    return;
  }

  // getBoundingClientRect() reflects the CURRENT VISUAL position, transform
  // included -- if a previous FLIP's 180ms transition is still mid-flight
  // when this runs, every measurement below would read an animating, not
  // resting, position. Snapping every tracked element to its resting
  // position first guarantees both this and the FLIP diff below are
  // computed from stable layout, not a snapshot of an in-progress animation.
  const tracked = trackedPreviewEls(listEl);
  tracked.forEach((el) => {
    el.style.transition = "none";
    el.style.transform = "";
  });
  const before = flipRects(tracked);

  const wrap = container.classList.contains("group-tags--wrap");
  if (!activePlaceholder) {
    activePlaceholder = document.createElement("div");
    activePlaceholder.className = "drop-placeholder";
  } else if (activePlaceholder.parentElement) {
    activePlaceholder.parentElement.removeChild(activePlaceholder);
  }
  sizePlaceholder(activePlaceholder, wrap);
  container.insertBefore(activePlaceholder, [...container.children][localIndex] || null);
  previewListEl = listEl;
  previewTargetIndex = index;
  previewTargetContainer = container;

  playFlip(tracked, before);
}

// Drag moved somewhere with no live preview (off the board entirely) — drop
// the placeholder and let everything slide back to exactly where it started.
function revertReorderPreview() {
  if (!activePlaceholder) return;
  const listEl = previewListEl;
  const tracked = trackedPreviewEls(listEl);
  tracked.forEach((el) => {
    el.style.transition = "none";
    el.style.transform = "";
  });
  const before = flipRects(tracked);
  removeDropPlaceholder();
  playFlip(tracked, before);
}

function wireDropZone(bodyEl, listEl) {
  bodyEl.addEventListener("dragover", (e) => {
    if (!currentDrag) return;
    e.preventDefault();
    bodyEl.classList.add("drag-over");

    try {
      updateReorderPreview(listEl, e.clientX, e.clientY, currentDrag.ids);
    } catch (err) {
      // The preview is a cosmetic layer on top of the real drag session --
      // e.preventDefault() above has already run, so the drop itself stays
      // valid regardless. Never let a preview bug (this file's most complex
      // part) take the whole drag down with it; just drop the visual and
      // keep going, so drop() still gets a chance to commit correctly.
      console.error("drag preview failed, continuing without it:", err);
      removeDropPlaceholder();
    }
  });

  bodyEl.addEventListener("dragleave", (e) => {
    if (!bodyEl.contains(e.relatedTarget)) {
      bodyEl.classList.remove("drag-over");
      revertReorderPreview();
    }
  });

  bodyEl.addEventListener("drop", (e) => {
    e.preventDefault();
    bodyEl.classList.remove("drag-over");
    if (!currentDrag) return;

    const { ids } = currentDrag;

    // Commit exactly what the live preview last showed rather than
    // re-measuring independently -- see previewTargetIndex's declaration
    // for why a second measurement can disagree with the first. Guarded
    // the same way as the preview itself: a resolution failure here should
    // never leave the drag half-finished with no feedback at all.
    let dropIndex, dropContainer;
    try {
      if (previewListEl === listEl && previewTargetIndex !== null) {
        dropIndex = previewTargetIndex;
        dropContainer = previewTargetContainer;
      } else {
        const resolved = computeDropTarget(listEl, e.clientX, e.clientY, ids);
        dropIndex = resolved.index;
        dropContainer = resolved.container;
      }
    } catch (err) {
      console.error("drop position resolution failed:", err);
      return;
    }

    // Dropping a tag in among a different group's run should make it join
    // that group, not just sit physically between its tags while still
    // labeled with its old role -- renderGroupedList groups by contiguous
    // `role`, so a stale role on a relocated tag is exactly what splits the
    // target group into two runs around it. Adopt whatever role the landing
    // spot's .group-tags wrapper carries (see its `dataset.role`, set in
    // renderGroupedList). A dragged GROUP HEADER moves its whole run as one
    // block and keeps its own role -- it's relocating a group, not merging
    // into one.
    if (!currentDrag.isGroup) {
      const targetRole = dropContainer && dropContainer.dataset ? dropContainer.dataset.role : undefined;
      if (targetRole !== undefined) {
        // A job position's own Tools & Skills subsection is a second
        // .group-tags container sharing the same role (see appendGroup) --
        // dataset.section tells the two apart, so a tag dropped into it
        // picks up `section: "tools"` and one dropped back into the main
        // bullet list loses it again.
        const targetSection = dropContainer.dataset.section === "tools" ? "tools" : undefined;
        state.right.forEach((t) => {
          if (ids.includes(t.id)) {
            t.role = targetRole;
            if (targetSection) t.section = targetSection;
            else delete t.section;
          }
        });
      }
    }

    if (moveItems(ids, dropIndex)) {
      saveState();
      render();
    }
  });
}

// ---------- hooks used by chat.js ----------

window.Boards = {
  getRightTags: () => state.right,
  insertRightTag: (item, atIndex) => {
    const idx = Math.max(0, Math.min(atIndex, state.right.length));
    state.right.splice(idx, 0, item);
    saveState();
    render();
  },
  removeRightTag: (id) => {
    state.right = state.right.filter((t) => t.id !== id);
    saveState();
    render();
  },
};

// ---------- init ----------

const els = {};

function init() {
  els.rightList = document.getElementById("rightList");
  els.rightBody = document.getElementById("rightBody");
  els.rightCount = document.getElementById("rightCount");
  els.undoBtn = document.getElementById("undoBtn");
  els.redoBtn = document.getElementById("redoBtn");

  state = loadState();
  resetHistory(); // this load (fresh seed or a prior save) is the undo floor for this session
  render();

  wireDropZone(els.rightBody, els.rightList);

  els.undoBtn.addEventListener("click", undo);
  els.redoBtn.addEventListener("click", redo);
  document.getElementById("resetBtn").addEventListener("click", resetState);
  document.getElementById("closeBtn").addEventListener("click", () => window.close());

  // Ctrl/Cmd+Z / Ctrl/Cmd+Y (or +Shift+Z) -- skipped while an inline-edit
  // input/textarea is focused so this doesn't fight the browser's own
  // native text-undo inside whatever tag/header is being typed into.
  document.addEventListener("keydown", (e) => {
    const focusedTag = document.activeElement && document.activeElement.tagName;
    if (focusedTag === "INPUT" || focusedTag === "TEXTAREA") return;
    if (!(e.ctrlKey || e.metaKey)) return;
    const key = e.key.toLowerCase();
    if (key === "z" && !e.shiftKey) {
      e.preventDefault();
      undo();
    } else if (key === "y" || (key === "z" && e.shiftKey)) {
      e.preventDefault();
      redo();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
