// Resizable layout: a vertical handle between the two boards, a horizontal
// handle between the boards row and the chat box. Both just rewrite CSS
// custom properties (--left-w, --chat-h) on .app — the grid tracks that use
// them (see style.css) do the actual resizing, and everything inside each
// block (tag lists, chat messages) reflows naturally since they're already
// flex/overflow-based rather than fixed-size.
(function () {
  const LAYOUT_KEY = "cvExtLayout_v1";
  const DEFAULT_LEFT_W = 340; // must match style.css's var(--left-w, 340px) fallback
  const DEFAULT_CHAT_H = 260; // must match style.css's var(--chat-h, 260px) fallback
  const MIN_LEFT_W = 220;
  const MIN_RIGHT_W = 320;
  const MIN_CHAT_H = 120;
  const MIN_BOARDS_H = 220;
  const HANDLE_SIZE = 6;
  const HEADER_H = 52;

  const appEl = document.querySelector(".app");

  function loadLayout() {
    try {
      return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {};
    } catch {
      return {};
    }
  }

  function saveLayout(patch) {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ ...loadLayout(), ...patch }));
  }

  function currentLeftW() {
    const raw = parseFloat(getComputedStyle(appEl).getPropertyValue("--left-w"));
    return Number.isFinite(raw) ? raw : DEFAULT_LEFT_W;
  }

  function currentChatH() {
    const raw = parseFloat(getComputedStyle(appEl).getPropertyValue("--chat-h"));
    return Number.isFinite(raw) ? raw : DEFAULT_CHAT_H;
  }

  function applyLayout(layout) {
    if (layout.leftW) appEl.style.setProperty("--left-w", `${layout.leftW}px`);
    if (layout.chatH) appEl.style.setProperty("--chat-h", `${layout.chatH}px`);
  }

  // Shared pointer-drag wiring for a resize handle: `onMove(clientX, clientY)`
  // updates the live CSS var during the drag, `onEnd()` persists the result.
  function wireHandle(handle, cursor, onMove, onEnd) {
    let dragging = false;

    handle.addEventListener("pointerdown", (e) => {
      dragging = true;
      handle.classList.add("dragging");
      handle.setPointerCapture(e.pointerId);
      document.body.style.cursor = cursor;
      document.body.style.userSelect = "none";
      e.preventDefault();
    });

    handle.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      onMove(e.clientX, e.clientY);
    });

    function endDrag() {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      onEnd();
    }
    handle.addEventListener("pointerup", endDrag);
    handle.addEventListener("pointercancel", endDrag);
  }

  function wireColumnResizer() {
    const handle = document.getElementById("colResizer");
    const boardsEl = document.querySelector(".boards");

    wireHandle(
      handle,
      "col-resize",
      (clientX) => {
        const rect = boardsEl.getBoundingClientRect();
        const maxLeft = rect.width - MIN_RIGHT_W - HANDLE_SIZE;
        const newW = Math.max(MIN_LEFT_W, Math.min(clientX - rect.left, maxLeft));
        appEl.style.setProperty("--left-w", `${newW}px`);
      },
      () => saveLayout({ leftW: currentLeftW() })
    );
  }

  function wireRowResizer() {
    const handle = document.getElementById("rowResizer");

    wireHandle(
      handle,
      "row-resize",
      (clientX, clientY) => {
        const rect = appEl.getBoundingClientRect();
        const maxChatH = rect.height - HEADER_H - MIN_BOARDS_H - HANDLE_SIZE;
        const newH = Math.max(MIN_CHAT_H, Math.min(rect.bottom - clientY, maxChatH));
        appEl.style.setProperty("--chat-h", `${newH}px`);
      },
      () => saveLayout({ chatH: currentChatH() })
    );
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyLayout(loadLayout());
    wireColumnResizer();
    wireRowResizer();
  });
})();
