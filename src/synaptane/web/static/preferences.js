"use strict";
// Runs before the stylesheet: the preference causes no theme flash.
// Everything else is an enhancement: without JS, every page stays usable.
(() => {
  const root = document.documentElement;
  root.dataset.enhanced = "true";
  const prefix = `synaptane.ui.v1.${root.dataset.userKey}.`;
  const read = name => { try { return localStorage.getItem(prefix + name); } catch { return null; } };
  const write = (name, value) => { try { localStorage.setItem(prefix + name, value); } catch { /* Navigation remains usable. */ } };
  const session = {
    get: name => { try { return sessionStorage.getItem(prefix + name); } catch { return null; } },
    set: (name, value) => { try { sessionStorage.setItem(prefix + name, value); } catch { /* No storage: nothing. */ } },
    drop: name => { try { sessionStorage.removeItem(prefix + name); } catch { /* No storage: nothing. */ } },
  };
  const applyTheme = value => {
    const theme = ["light", "dark"].includes(value) ? value : "system";
    root.dataset.theme = theme;
    document.querySelectorAll("[data-theme-option]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.themeOption === theme));
    });
  };
  applyTheme(read("theme"));
  const narrow = () => window.matchMedia("(max-width: 900px)").matches;
  // On mobile, the drawer is closed from the first render (no animation on load).
  if (narrow() || read("sidebar-collapsed") === "true") root.dataset.sidebarCollapsed = "true";

  // --- Mermaid diagrams -------------------------------------------------------------------------
  // The code stays in the page: without JS, or if the text cannot be drawn, it is what you read.
  // Each drawing is made in a sandboxed frame (see server.py, /diagram), when it comes near.
  // The editor's preview reuses it: a diagram being edited keeps its frame, which draws the new
  // text (the last drawing stays, dimmed, while the text cannot be drawn).
  const diagramFrames = new WeakMap();  // code block (pre) → its frame
  const diagramStates = new WeakMap();  // frame → { block, text, ready, drawn, stop }
  const sendDiagram = frame => {
    const state = diagramStates.get(frame);
    const dark = root.dataset.theme === "dark" || (root.dataset.theme === "system"
      && window.matchMedia("(prefers-color-scheme: dark)").matches);
    // The same color scheme on both sides, or the browser paints the frame opaque.
    frame.style.colorScheme = dark ? "dark" : "light";
    frame.contentWindow.postMessage({ text: state.text, dark }, "*");
  };
  const removeDiagram = frame => {
    diagramStates.get(frame)?.stop();
    frame.remove();
  };
  const drawDiagram = code => {
    const block = code.closest("pre");
    const frame = document.createElement("iframe");
    frame.className = "diagram-frame";
    frame.src = "/diagram";
    frame.title = root.dataset.diagramLabel;
    frame.setAttribute("sandbox", "allow-scripts");
    const state = { block, text: code.textContent, ready: false, drawn: false };
    const listen = event => {
      if (!frame.isConnected) { state.stop(); return; }
      if (event.source !== frame.contentWindow) return;
      const { ready, height, failed } = event.data ?? {};
      if (ready) {
        state.ready = true;
        sendDiagram(frame);
      } else if (failed) {
        if (state.drawn) frame.classList.add("diagram-stale");
        else removeDiagram(frame);
      } else if (Number.isFinite(height) && height > 0) {
        frame.style.height = `${Math.min(height, 4000)}px`;
        frame.classList.remove("diagram-stale");
        state.drawn = true;
        state.block.hidden = true;
      }
    };
    state.stop = () => window.removeEventListener("message", listen);
    diagramStates.set(frame, state);
    diagramFrames.set(block, frame);
    window.addEventListener("message", listen);
    block.after(frame);
  };
  const redrawDiagram = (frame, block) => {
    const state = diagramStates.get(frame);
    diagramFrames.set(block, frame);
    state.block = block;
    state.text = block.textContent;
    if (state.drawn) block.hidden = true;
    if (state.ready) sendDiagram(frame);
  };
  let nearDiagrams = null;
  const observeDiagrams = scope => {
    const blocks = [...scope.querySelectorAll(".markdown code.language-mermaid")]
      .filter(code => !diagramFrames.has(code.closest("pre")));
    if (!blocks.length) return;
    nearDiagrams ??= new IntersectionObserver((entries, observer) => {
      entries.filter(entry => entry.isIntersecting).forEach(entry => {
        observer.unobserve(entry.target);
        if (entry.target.isConnected) drawDiagram(entry.target);
      });
    }, { rootMargin: "400px" });
    blocks.forEach(code => nearDiagrams.observe(code));
  };
  const forgetDiagrams = node => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    node.querySelectorAll("code.language-mermaid").forEach(code => nearDiagrams?.unobserve(code));
    node.querySelectorAll(".diagram-frame").forEach(removeDiagram);
  };
  const setupDiagrams = () => observeDiagrams(document);

  // --- First steps of a space: can be hidden, remembered in this browser -------------------------
  const setupFirstSteps = () => {
    const section = document.querySelector("[data-first-steps]");
    if (!section) return;
    const name = `first-steps-hidden.${section.dataset.firstSteps}`;
    if (read(name) === "true") { section.remove(); return; }
    const close = section.querySelector("[data-first-steps-close]");
    close.hidden = false;
    close.addEventListener("click", () => { write(name, "true"); section.remove(); });
  };

  // --- Navigation (sidebar, drawer on mobile) --------------------------------------------------
  const setupSidebar = () => {
    const sidebar = document.getElementById("workspace-sidebar");
    const toggle = document.getElementById("sidebar-toggle");
    if (!sidebar || !toggle) return;
    const apply = collapsed => {
      root.dataset.sidebarCollapsed = String(collapsed);
      sidebar.inert = collapsed;
      sidebar.setAttribute("aria-hidden", String(collapsed));
      toggle.setAttribute("aria-expanded", String(!collapsed));
    };
    // On mobile, the drawer is always closed on load and its state is not remembered.
    const set = collapsed => { apply(collapsed); if (!narrow()) write("sidebar-collapsed", String(collapsed)); };
    apply(narrow() ? true : root.dataset.sidebarCollapsed === "true");
    toggle.addEventListener("click", () => set(root.dataset.sidebarCollapsed !== "true"));
    // Window widened or narrowed (rotated tablet): drawer closed on mobile, remembered state
    // on large screens.
    window.matchMedia("(max-width: 900px)").addEventListener("change", event => {
      apply(event.matches ? true : read("sidebar-collapsed") === "true");
    });
    document.querySelectorAll("[data-sidebar-close]").forEach(button => button.addEventListener("click", () => { set(true); toggle.focus(); }));
    document.querySelector("[data-sidebar-scrim]")?.addEventListener("click", () => set(true));
    document.addEventListener("keydown", event => {
      // Escape closes the drawer (mobile); in a field, it keeps its usual role.
      if (event.key !== "Escape" || !narrow() || root.dataset.sidebarCollapsed === "true") return;
      if (event.target.closest("input, textarea, select")) return;
      set(true);
      toggle.focus();
    });
  };

  // --- Confirmations -----------------------------------------------------------------------------
  const setupConfirmations = () => {
    document.addEventListener("submit", event => {
      const form = event.target;
      const message = event.submitter?.dataset.confirm || form.dataset.confirm;
      if (message && !window.confirm(message)) event.preventDefault();
    }, true);
  };

  // --- Submit: no double submission, input restored after an error ----------------------------------
  // Never a secret in browser storage: passwords, files, and fields marked `data-no-draft`
  // or `autocomplete="off"` (private key, token…).
  const fields = form => [...form.elements].filter(field =>
    field.name && field.name !== "csrf" && !["password", "file", "hidden", "submit", "button"].includes(field.type)
    && !("noDraft" in field.dataset) && field.getAttribute("autocomplete") !== "off");
  const setupForms = () => {
    const failed = document.querySelector("[data-flash='error']");
    const pending = session.get("draft");
    if (pending) {
      session.drop("draft");
      try {
        const draft = JSON.parse(pending);
        const form = failed && document.querySelector(`form[action="${CSS.escape(draft.action)}"]`);
        if (form) {
          fields(form).forEach(field => {
            if (!(field.name in draft.values)) return;
            if (field.type === "checkbox" || field.type === "radio") field.checked = draft.values[field.name].includes(field.value);
            else field.value = draft.values[field.name][0] ?? "";
          });
          fields(form)[0]?.focus();
        }
      } catch { /* Unreadable draft: ignored. */ }
    }
    document.addEventListener("submit", event => {
      if (event.defaultPrevented) return;
      const form = event.target;
      if (form.method.toLowerCase() !== "post") return;
      const values = {};
      fields(form).forEach(field => {
        if ((field.type === "checkbox" || field.type === "radio") && !field.checked) { values[field.name] ??= []; return; }
        (values[field.name] ??= []).push(field.value);
      });
      session.set("draft", JSON.stringify({ action: form.getAttribute("action"), values }));
      const button = event.submitter;
      if (button) {
        setTimeout(() => { button.disabled = true; button.setAttribute("aria-busy", "true"); }, 0);
        // A long operation (a repository created on GitHub) says how long it may take.
        const busy = 1000 * (Number(form.dataset.busySeconds) || 15);
        setTimeout(() => { button.disabled = false; button.removeAttribute("aria-busy"); }, busy);
      }
    });
  };

  // --- Editor: warn before leaving with unsaved text ----------------------------------------------
  const setupEditor = () => {
    document.querySelectorAll("textarea.editor").forEach(editor => {
      const form = editor.form;
      let dirty = false;
      editor.addEventListener("input", () => { dirty = true; });
      form?.addEventListener("submit", () => { dirty = false; });
      window.addEventListener("beforeunload", event => { if (dirty) event.preventDefault(); });
    });
  };

  // --- Editor: preview of the text, as the page will read -----------------------------------------
  // The server renders it (/-/preview: the reading view's rendering, nothing saved); "Write",
  // "Preview", or both side by side on a large screen (remembered in this browser). Side by
  // side, it follows the typing after a pause at least as long as the last rendering; one
  // request at a time: the latest text is sent once the answer is in. Only the blocks that
  // changed are replaced. Links open in a new tab, relative ones resolved from the page's reading
  // address, anchors stay inside the preview: the text being written is never left.
  // Without JS: the editor alone.
  const previewSource = node => (node.nodeType === Node.ELEMENT_NODE ? node.outerHTML : `#${node.textContent}`);
  const isolatePreview = (node, base) => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const page = new URL(base || window.location.href, window.location.href);
    const all = selector => [node, ...node.querySelectorAll(selector)].filter(el => el.matches(selector));
    const relative = value => !/^([a-z][a-z0-9+.-]*:|\/)/i.test(value);
    all("[id]").forEach(el => { el.id = `preview-${el.id}`; });
    all("a[href]").forEach(link => {
      const href = link.getAttribute("href");
      if (href.startsWith("#")) { link.setAttribute("href", `#preview-${href.slice(1)}`); return; }
      if (relative(href)) link.setAttribute("href", new URL(href, page).href);
      link.target = "_blank";
    });
    all("img[src]").forEach(image => {
      const src = image.getAttribute("src");
      if (relative(src)) image.setAttribute("src", new URL(src, page).href);
    });
  };
  const isDiagramBlock = node => node.nodeType === Node.ELEMENT_NODE && node.matches("pre")
    && node.querySelector(":scope > code.language-mermaid") !== null;
  const patchPreview = (output, shown, html, base) => {
    const fresh = document.createElement("template");
    fresh.innerHTML = html;
    const nodes = [...fresh.content.childNodes];
    const sources = nodes.map(previewSource);
    let start = 0;
    while (start < shown.length && start < nodes.length && shown[start].source === sources[start]) start += 1;
    let end = 0;
    while (end < shown.length - start && end < nodes.length - start
      && shown[shown.length - 1 - end].source === sources[nodes.length - 1 - end]) end += 1;
    const anchor = end ? shown[shown.length - end].node : null;
    const gone = shown.slice(start, shown.length - end).map(entry => entry.node);
    const added = nodes.slice(start, nodes.length - end);
    // A diagram being edited: its frame stays where it is and draws the new text.
    const kept = gone.find(node => isDiagramBlock(node) && diagramFrames.has(node));
    const target = kept && added.find(isDiagramBlock);
    gone.forEach(node => {
      forgetDiagrams(node);
      const frame = diagramFrames.get(node);
      if (frame && !(node === kept && target)) removeDiagram(frame);
      node.remove();
    });
    added.forEach(node => isolatePreview(node, base));
    if (target) {
      const frame = diagramFrames.get(kept);
      const at = added.indexOf(target);
      added.slice(0, at + 1).forEach(node => output.insertBefore(node, frame));
      added.slice(at + 1).forEach(node => output.insertBefore(node, anchor));
      redrawDiagram(frame, target);
    } else {
      added.forEach(node => output.insertBefore(node, anchor));
    }
    shown.splice(start, gone.length, ...added.map((node, i) => ({ source: sources[start + i], node })));
    added.forEach(node => { if (node.nodeType === Node.ELEMENT_NODE) observeDiagrams(node); });
  };
  const setupPreview = () => {
    document.querySelectorAll("[data-preview]").forEach(box => {
      const editor = box.querySelector("textarea");
      const form = editor?.form;
      const modes = box.querySelector(".editor-modes");
      if (!form || !modes || !window.fetch || !window.FormData) return;
      const panes = box.querySelector(".editor-panes");
      const pane = box.querySelector("[data-preview-pane]");
      const output = box.querySelector("[data-preview-output]");
      const status = box.querySelector("[data-preview-status]");
      const buttons = [...modes.querySelectorAll("[data-preview-mode]")];
      const wide = window.matchMedia("(min-width: 1280px)");
      const max = Number(box.dataset.previewMax) || Infinity;
      const shown = [];
      let mode = "write";
      let shownKey = null;  // text and place shown (or refused for good: too long)
      let base = "";  // reading address of the page shown
      let busy = false;  // a request is on its way: the next one waits for its answer
      let again = false;
      let hold = 0;  // after a 429: wait longer
      let pause = 700;
      let timer = 0;
      const where = () => (box.dataset.previewPath
        ? { path: box.dataset.previewPath }
        : { folder: form.elements.folder?.value ?? "", name: form.elements.name?.value ?? "" });
      // A new page that will not be Markdown (notes.txt, page.html): nothing to preview.
      const markdown = () => {
        if (box.dataset.previewPath) return true;
        const last = (form.elements.name?.value ?? "").trim().replace(/\/+$/, "").split("/").pop();
        const suffix = /[^./]\.([^./]*)$/.exec(last)?.[1] ?? "";
        return !suffix || ["md", "markdown"].includes(suffix.toLowerCase());
      };
      const say = message => { status.textContent = message; };
      const schedule = delay => { clearTimeout(timer); timer = setTimeout(send, delay ?? pause); };
      const send = async () => {
        clearTimeout(timer);
        if (mode === "write") return;
        if (busy) { again = true; return; }
        const place = where();
        const key = JSON.stringify([editor.value, place]);
        if (key === shownKey) return;
        if (!editor.value.trim()) {
          patchPreview(output, shown, "", base);
          say(box.dataset.previewEmpty);
          shownKey = key;
          return;
        }
        if (editor.value.length > max) { say(box.dataset.previewTooLong); shownKey = key; return; }
        busy = true;
        pane.setAttribute("aria-busy", "true");
        const started = performance.now();
        try {
          const body = new FormData();
          body.set("csrf", form.elements.csrf.value);
          body.set("text", editor.value);
          Object.entries(place).forEach(([name, value]) => body.set(name, value));
          const response = await fetch(box.dataset.preview, { method: "POST", body, credentials: "same-origin" });
          if (response.redirected) { say(box.dataset.previewExpired); return; }  // sign-in page
          if (response.status === 429) { again = true; hold = 2000; return; }  // this account's other preview first
          const text = await response.text();
          if (!response.ok) {
            const plain = (response.headers.get("content-type") || "").startsWith("text/plain");
            if (plain && text) say(text);
            else if ([400, 413].includes(response.status)) say(box.dataset.previewTooLong);  // field too heavy
            else { say(box.dataset.previewFailed); return; }
            shownKey = key;  // the same text would be refused again
            return;
          }
          const page = response.headers.get("X-Preview-Page") || "";
          if (page !== base) patchPreview(output, shown, "", base);  // relative links change
          base = page;
          patchPreview(output, shown, text, base);
          say("");
          shownKey = key;
          pause = Math.max(700, performance.now() - started);
        } catch {
          say(box.dataset.previewFailed);  // network: the next change or click tries again
        } finally {
          busy = false;
          pane.removeAttribute("aria-busy");
          if (again) { again = false; schedule(Math.max(pause, hold)); hold = 0; }
        }
      };
      const apply = (next, now = true) => {
        mode = next;
        if (mode !== "write" && !markdown()) mode = "write";
        if (mode === "split" && !wide.matches) mode = "write";
        buttons.forEach(button => {
          button.setAttribute("aria-pressed", String(button.dataset.previewMode === mode));
          if (button.dataset.previewMode !== "write") button.disabled = !markdown();
        });
        panes.classList.toggle("split", mode === "split");
        editor.hidden = mode === "preview";
        pane.hidden = mode === "write";
        if (mode !== "write") { if (now) send(); else schedule(); }
      };
      // Narrowed window, or a name that is not Markdown: back to the text; then side by side
      // again if it was chosen.
      const settle = () => apply(mode === "write" && read("editor-split") === "true" ? "split" : mode, false);
      buttons.forEach(button => button.addEventListener("click", () => {
        const next = button.dataset.previewMode;
        if (next === "split" || mode === "split") write("editor-split", String(next === "split"));
        apply(next);
      }));
      editor.addEventListener("input", () => { if (mode === "split") schedule(); });
      form.elements.folder?.addEventListener?.("input", () => { if (mode !== "write") schedule(); });
      form.elements.name?.addEventListener?.("input", settle);
      wide.addEventListener("change", settle);
      modes.hidden = false;
      apply(read("editor-split") === "true" ? "split" : "write");
    });
  };

  // --- Purpose: a template prefills the editor (confirmation if it holds other text) -----------
  // Without JS, the form reloads the page with the template in the editor: nothing is saved.
  const setupVocationTemplate = () => {
    document.querySelectorAll("[data-vocation-template]").forEach(form => {
      const editor = document.getElementById(form.dataset.target);
      let texts = {};
      try { texts = JSON.parse(form.dataset.texts || "{}"); } catch { /* normal submit */ }
      if (!editor) return;
      form.addEventListener("submit", event => {
        const text = texts[form.elements.template.value];
        if (typeof text !== "string") return;
        event.preventDefault();
        const current = editor.value.trim();
        if (current && current !== text.trim() && !window.confirm(form.dataset.confirmReplace)) return;
        editor.value = text;
        editor.dispatchEvent(new Event("input", { bubbles: true }));
        editor.focus();
        editor.setSelectionRange(0, 0);
        editor.scrollTop = 0;
      });
    });
  };

  // --- Dates in the browser's local time ----------------------------------------------------------
  const setupDates = () => {
    const lang = root.lang || "en";
    const full = new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" });
    const day = new Intl.DateTimeFormat(lang, { dateStyle: "medium" });
    // "Oct 3" this year, "Oct 3, 2025" otherwise: a date without time (verification).
    const short = new Intl.DateTimeFormat(lang, { day: "numeric", month: "short" });
    const year = new Date().getFullYear();
    document.querySelectorAll("time[data-local]").forEach(element => {
      const date = new Date(element.getAttribute("datetime"));
      if (Number.isNaN(date.getTime())) return;
      if (element.dataset.local === "day") {
        element.textContent = (date.getFullYear() === year ? short : day).format(date);
        return;
      }
      element.title = date.toISOString();
      element.textContent = (element.dataset.local === "date" ? day : full).format(date);
    });
  };

  // --- Tree: folders expanded on demand ------------------------------------------------------------
  const setupTree = () => {
    document.addEventListener("click", async event => {
      const toggle = event.target.closest(".tree-toggle");
      if (!toggle) return;
      const item = toggle.closest("li");
      const open = toggle.getAttribute("aria-expanded") === "true";
      let children = item.querySelector(":scope > .space-tree-list");
      if (!open && !children) {
        toggle.setAttribute("aria-busy", "true");
        try {
          const response = await fetch(toggle.dataset.tree, { credentials: "same-origin" });
          if (!response.ok) return;
          const holder = document.createElement("div");
          holder.innerHTML = await response.text();
          children = holder.querySelector(".space-tree-list");
          if (children) item.append(children);
        } finally { toggle.removeAttribute("aria-busy"); }
      }
      if (children) children.hidden = open;
      toggle.setAttribute("aria-expanded", String(!open));
    });
    // Current page visible in the tree: scroll the container only.
    // `scrollIntoView` also moved the tab navigation starting point (Chromium): the first
    // Tab skipped the skip link and landed in the middle of the tree.
    const current = document.querySelector(".space-tree [aria-current]");
    const box = current?.closest(".sidebar-tree");
    if (current && box) {
      const top = current.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
      if (top < box.scrollTop || top + current.offsetHeight > box.scrollTop + box.clientHeight) {
        box.scrollTop = Math.max(top - box.clientHeight / 2, 0);
      }
    }
  };

  // --- "See also": related pages, loaded after the page ----------------------------------------
  // The computation (meaning, words, links) never delays the document display; nothing related
  // (204 response) or an error: the panel stays hidden. Without JS, it does not appear.
  const setupRelated = () => {
    const panel = document.querySelector("[data-related]");
    if (!panel) return;
    fetch(panel.dataset.related, { credentials: "same-origin" })
      .then(async response => {
        if (response.status !== 200 || response.redirected) return; // session expired: login page
        const html = await response.text();
        if (!html.trim()) return;
        panel.innerHTML = html;
        panel.hidden = false;
      })
      .catch(() => { /* Optional panel: the page is complete without it. */ });
  };

  // --- Copy --------------------------------------------------------------------------------------
  const setupCopy = () => {
    document.querySelectorAll("[data-copy]").forEach(button => {
      button.hidden = false;
      button.addEventListener("click", async () => {
        const target = document.getElementById(button.dataset.copy);
        try {
          await navigator.clipboard.writeText(target ? target.textContent.trim() : "");
          const label = button.textContent;
          button.textContent = button.dataset.copied || "✓";
          setTimeout(() => { button.textContent = label; }, 1500);
        } catch { target && window.getSelection()?.selectAllChildren(target); }
      });
    });
  };

  // --- Assistant connection test: waits for an MCP call for 5 minutes -----------------------------
  const setupAssistantTest = () => {
    const box = document.querySelector("[data-assistant-test]");
    const status = box?.querySelector("[data-assistant-status]");
    if (!box || !status) return;
    status.textContent = status.dataset.waiting;
    const started = Date.now();
    const poll = async () => {
      try {
        const url = `${box.dataset.url}?since=${encodeURIComponent(box.dataset.since)}`;
        const response = await fetch(url, { credentials: "same-origin" });
        const uses = response.ok ? (await response.json()).uses : [];
        if (uses.length) {
          status.textContent = status.dataset.received.replace("{via}", uses[0].label || uses[0].via);
          status.classList.add("status-ok");
          return;
        }
      } catch { /* network: try again */ }
      if (Date.now() - started > 5 * 60 * 1000) { status.textContent = status.dataset.timeout; return; }
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  };

  // --- Subscription test: run by the worker; the page reloads when it is done ---------------------
  const setupPendingTest = () => {
    const box = document.querySelector("[data-test-pending]");
    if (!box) return;
    const started = Date.now();
    const poll = async () => {
      try {
        const response = await fetch(box.dataset.testPending, { credentials: "same-origin" });
        if (response.ok && !(await response.json()).pending) { window.location.reload(); return; }
      } catch { /* network: try again */ }
      if (Date.now() - started < 10 * 60 * 1000) setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  };

  // --- Tidy-up log: follow a requested run, compact cards on mobile ------------------------------
  // While a run is requested or in progress, light polling every 3 s; the page reloads as
  // soon as the state changes (start, then result). On mobile, beyond three cards, only the
  // most recent stays expanded: the others collapse to their title.
  const setupTidyJournal = () => {
    const state = document.querySelector("[data-tidy-state]");
    if (state) {
      document.querySelectorAll("[data-live-hint]").forEach(hint => { hint.hidden = false; });
      const started = Date.now();
      const poll = async () => {
        try {
          const response = await fetch(state.dataset.tidyState, { credentials: "same-origin", cache: "no-store" });
          if (response.ok) {
            const data = await response.json();
            if (data.version !== state.dataset.version) { window.location.reload(); return; }
            if (!data.poll) return;
          }
        } catch { /* network: try again */ }
        if (Date.now() - started < 60 * 60 * 1000) setTimeout(poll, 3000);
      };
      setTimeout(poll, 3000);
    }
    const journal = document.querySelector("[data-tidy-journal]");
    const cards = journal ? [...journal.querySelectorAll(":scope > .tidy-run")] : [];
    if (!narrow() || cards.length <= 3) return;
    journal.dataset.compact = "true";
    cards.slice(1).forEach(card => {
      const head = card.querySelector(".tidy-run-head");
      const body = card.querySelector(".tidy-run-body");
      if (!head || !body || !body.id || !body.children.length) return;
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "button button-small tidy-run-toggle";
      toggle.textContent = journal.dataset.more || "…";
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-controls", body.id);
      body.hidden = true;
      toggle.addEventListener("click", () => {
        const open = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", String(open));
        body.hidden = !open;
      });
      head.append(toggle);
    });
  };

  // --- Access restrictions: checking a space selects "only these spaces" --------------------------
  const setupAccessLimits = () => {
    document.querySelectorAll("[data-access-limits]").forEach(box => {
      const all = box.querySelector("input[name=scope][value=all]");
      const some = box.querySelector("input[name=scope][value=some]");
      const spaces = [...box.querySelectorAll("input[name=space]")];
      spaces.forEach(input => input.addEventListener("change", () => {
        if (input.checked && some) some.checked = true;
      }));
      all?.addEventListener("change", () => { if (all.checked) spaces.forEach(input => { input.checked = false; }); });
    });
  };

  // --- Fields by AI provider (without JS, everything stays visible) ------------------------------
  const setupProviderFields = () => {
    const select = document.getElementById("ai-provider") || document.getElementById("emb-provider");
    if (!select) return;
    const model = document.getElementById(select.dataset.modelField || "");
    const update = () => {
      document.querySelectorAll("[data-providers]").forEach(field => {
        const hidden = !field.dataset.providers.split(" ").includes(select.value);
        const input = field.querySelector("input[type=text], input:not([type])");
        field.hidden = hidden && !(input && input.value);  // an address already entered stays visible
      });
      const fallback = select.selectedOptions[0] && select.selectedOptions[0].dataset.model;
      if (model && fallback !== undefined) model.placeholder = fallback;  // default model
    };
    select.addEventListener("change", update);
    update();
  };

  // --- New space: existing repository, or created by Synaptane (without JS, everything stays
  // visible). The repository name proposed is the identifier, or the name made into one.
  const setupRepoMode = () => {
    const box = document.querySelector("[data-repo-mode]");
    if (!box) return;
    const form = box.closest("form");
    const provider = box.querySelector("#repo-provider");
    const repoName = box.querySelector("[data-repo-name]");
    const slug = form.querySelector("#space-slug");
    const name = form.querySelector("#space-name");
    const update = () => {
      const mode = box.querySelector("input[name=repo_mode]:checked")?.value || "existing";
      form.querySelectorAll("[data-repo-when]").forEach(part => {
        part.hidden = part.dataset.repoWhen !== mode;
        part.querySelectorAll("[data-required]").forEach(input => { input.required = !part.hidden; });
      });
      box.querySelectorAll("[data-repo-provider]").forEach(field => { field.hidden = field.dataset.repoProvider !== provider.value; });
    };
    const propose = () => {
      const fromName = (name?.value || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
        .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
      repoName.placeholder = slug?.value || fromName || "infra";
    };
    box.addEventListener("change", update);
    slug?.addEventListener("input", propose);
    name?.addEventListener("input", propose);
    update();
    propose();
  };

  // --- File fields: names of the chosen files, in the page language ----------------------------
  // Several files: each selection adds to the list (instead of replacing it), and each file
  // can be removed; the submitted field stays the same (DataTransfer).
  const [byteUnit, kibUnit, mibUnit] = document.documentElement.lang === "fr"
    ? ["o", "Kio", "Mio"] : ["B", "KiB", "MiB"];  // as the `filesize` filter
  const fileSize = bytes => bytes < 1024 ? `${bytes} ${byteUnit}`
    : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} ${kibUnit}`
    : `${(bytes / 1048576).toFixed(1)} ${mibUnit}`;
  const setupFilePickers = () => {
    document.querySelectorAll(".file-picker").forEach(picker => {
      const input = picker.querySelector("input[type=file]");
      const names = picker.querySelector("[data-file-names]");
      const list = picker.querySelector("[data-file-list]");
      if (!input || !names) return;
      if (!input.multiple || !list || typeof DataTransfer === "undefined") {
        const show = () => { names.textContent = [...input.files].map(f => f.name).join(", ") || names.dataset.empty; };
        input.addEventListener("change", show);
        show();
        return;
      }
      const chosen = [];
      const key = file => `${file.name}|${file.size}|${file.lastModified}`;
      const sync = () => {
        const transfer = new DataTransfer();
        chosen.forEach(file => transfer.items.add(file));
        input.files = transfer.files;
        list.replaceChildren(...chosen.map((file, index) => {
          const item = document.createElement("li");
          const label = document.createElement("span");
          label.className = "file-picked-name";
          label.textContent = file.name;
          const size = document.createElement("span");
          size.className = "muted";
          size.textContent = fileSize(file.size);
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "icon-button";
          remove.textContent = "×";
          remove.setAttribute("aria-label", (picker.dataset.remove || "{name}").replace("{name}", file.name));
          remove.addEventListener("click", () => { chosen.splice(index, 1); sync(); input.focus(); });
          item.append(label, size, remove);
          return item;
        }));
        list.hidden = chosen.length === 0;
        names.textContent = chosen.length === 0 ? names.dataset.empty
          : chosen.length === 1 ? picker.dataset.one : (picker.dataset.many || "{n}").replace("{n}", chosen.length);
      };
      input.addEventListener("change", () => {
        const seen = new Set(chosen.map(key));
        [...input.files].forEach(file => { if (!seen.has(key(file))) { chosen.push(file); seen.add(key(file)); } });
        sync();
      });
      sync();
    });
  };

  // --- Dropdown menus (details): closed by Escape or an outside click ----------------------------------
  const setupMenus = () => {
    const close = except => document.querySelectorAll("details.menu[open]").forEach(menu => { if (menu !== except) menu.open = false; });
    document.addEventListener("click", event => close(event.target.closest("details.menu")));
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      const open = document.querySelector("details.menu[open]");
      if (open) { open.open = false; open.querySelector("summary")?.focus(); }
    });
  };

  // --- Breadcrumbs: when space runs out, the path folds into "…" ----------------------------
  // Levels disappear from the left (organization, space, folders); on hover, focus or click,
  // "…" shows them all, clickable. Only then do the document details, then the page name,
  // shrink (crumbs-tight class).
  const setupCrumbs = () => {
    const bar = document.querySelector(".workspace-topbar");
    const nav = bar && bar.querySelector(".topbar-crumbs");
    if (!nav) return;
    const items = [...nav.children].filter(el => el.tagName.toLowerCase() !== "svg");
    const ancestors = items.slice(0, -1).filter(el => el.tagName === "A");
    if (!ancestors.length) return;
    const more = document.createElement("span");
    more.className = "crumb-more";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "icon-button";
    button.textContent = "…";
    button.setAttribute("aria-label", nav.dataset.more || "…");
    button.setAttribute("aria-expanded", "false");
    const menu = document.createElement("div");
    menu.className = "crumb-menu";
    more.append(button, menu);
    nav.before(more);
    const close = () => { more.classList.remove("open"); button.setAttribute("aria-expanded", "false"); };
    button.addEventListener("click", () => {
      const open = more.classList.toggle("open");
      button.setAttribute("aria-expanded", String(open));
    });
    // Display follows `.open` (and mouse hover, in CSS): by touch, a second tap closes; by
    // keyboard, Escape closes and returns focus to the button, as does leaving the menu.
    document.addEventListener("click", event => { if (!more.contains(event.target)) close(); });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape" || !more.classList.contains("open")) return;
      const inside = more.contains(document.activeElement);
      close();
      if (inside) button.focus();
    });
    more.addEventListener("focusout", event => { if (!more.contains(event.relatedTarget)) close(); });
    const separator = el => {
      const before = el.previousElementSibling;
      return before && before.tagName.toLowerCase() === "svg" ? before : null;
    };
    const overflowing = () => nav.scrollWidth > nav.clientWidth + 1;
    const meta = bar.querySelector(".topbar-meta");
    // Room left for the breadcrumbs when the document details may disappear: everything,
    // except the menu button, "…" and the action buttons.
    const roomForCrumbs = () => {
      const style = getComputedStyle(bar);
      const shown = [...bar.children].filter(el => !el.hidden && getComputedStyle(el).display !== "none");
      const others = shown.filter(el => el !== nav).reduce((sum, el) => sum + el.getBoundingClientRect().width, 0);
      const gaps = (parseFloat(style.columnGap) || 0) * (shown.length - 1);
      const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      const metaWidth = meta ? meta.getBoundingClientRect().width : 0;
      return Math.max(bar.clientWidth - padding - gaps - others + metaWidth - 1, 40);
    };
    const layout = () => {
      bar.classList.remove("crumbs-tight");
      nav.style.flex = "";
      nav.style.maxWidth = "";
      ancestors.forEach(el => { el.hidden = false; const sep = separator(el); if (sep) sep.style.display = ""; });
      more.hidden = true;
      let hidden = 0;
      while (hidden < ancestors.length && overflowing()) {
        more.hidden = false;
        const el = ancestors[hidden];
        el.hidden = true;
        const sep = separator(el);
        if (sep) sep.style.display = "none";
        hidden += 1;
      }
      menu.replaceChildren(...ancestors.slice(0, hidden).map((el, depth) => {
        const link = el.cloneNode(true);
        link.hidden = false;
        link.style.paddingLeft = `${10 + depth * 14}px`;
        return link;
      }));
      if (overflowing()) {
        // Nothing left to fold: the page name keeps its width, the document details shrink;
        // the name only shrinks as a last resort.
        nav.style.flex = "0 0 auto";
        nav.style.maxWidth = `${roomForCrumbs()}px`;
        bar.classList.add("crumbs-tight");
      }
    };
    let pending = false;
    const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; layout(); }); } };
    if ("ResizeObserver" in window) new ResizeObserver(schedule).observe(bar);
    else window.addEventListener("resize", schedule);
    layout();
  };

  // --- Page outline: current section highlighted, collapsed on small screens ----------------
  const setupToc = () => {
    const toc = document.querySelector(".doc-toc");
    if (!toc) return;
    const small = window.matchMedia("(max-width: 1100px)");
    if (small.matches) toc.open = false;
    // Window enlarged: the outline goes back beside the text, so open; narrowed: collapsed.
    small.addEventListener("change", event => { toc.open = !event.matches; });
    toc.querySelector("summary").addEventListener("click", event => {
      if (!small.matches && toc.open) event.preventDefault();  // beside the text: always open
    });
    const links = [...toc.querySelectorAll("a[href^='#']")];
    const headings = links.map(link => document.getElementById(decodeURIComponent(link.hash.slice(1))));
    let pending = false;
    const update = () => {
      pending = false;
      const limit = (parseInt(getComputedStyle(root).getPropertyValue("--topbar-height"), 10) || 52) + 24;
      let current = -1;
      headings.forEach((heading, index) => { if (heading && heading.getBoundingClientRect().top <= limit) current = index; });
      links.forEach((link, index) => {
        if (index === Math.max(current, 0)) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
    };
    window.addEventListener("scroll", () => { if (!pending) { pending = true; requestAnimationFrame(update); } }, { passive: true });
    links.forEach(link => link.addEventListener("click", () => { if (small.matches) toc.open = false; }));
    update();
  };

  // --- Search palette: top bar magnifier, Ctrl/Cmd+K or "/" ------------------------------------
  // Live titles (JSON from /search/titles), in the current space or everywhere. Enter opens the
  // chosen page; "See all results" submits the form to /search. Without <dialog> or JS, the
  // magnifier is a plain link to the search page.
  const setupPalette = () => {
    const dialog = document.getElementById("search-palette");
    if (!dialog || typeof dialog.showModal !== "function") return;
    const form = dialog.querySelector("form");
    const input = dialog.querySelector("#palette-q");
    const list = dialog.querySelector("#palette-results");
    const status = dialog.querySelector("[data-palette-status]");
    if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
      document.querySelectorAll("[data-shortcut-label]").forEach(label => { label.textContent = "⌘ K"; });
    }
    let opener = null;
    let timer = 0;
    let controller = null;
    let active = -1;
    let asked = "";  // query in progress or displayed
    let shown = "";  // query whose results are displayed
    const scope = () => form.querySelector("input[name=space]:checked")?.value ?? "";
    const key = () => `${scope()}|${input.value.trim()}`;
    const options = () => [...list.querySelectorAll("[role=option]")];
    const highlight = index => {
      const all = options();
      active = all.length ? (index + all.length) % all.length : -1;
      all.forEach((option, i) => option.setAttribute("aria-selected", String(i === active)));
      if (active < 0) { input.removeAttribute("aria-activedescendant"); return; }
      input.setAttribute("aria-activedescendant", all[active].id);
      all[active].scrollIntoView({ block: "nearest" });
    };
    const show = (results, message, query) => {
      list.replaceChildren(...results.map((hit, index) => {
        const item = document.createElement("li");
        item.id = `palette-option-${index}`;
        item.setAttribute("role", "option");
        const link = document.createElement("a");
        link.href = hit.url;
        link.tabIndex = -1;
        const title = document.createElement("span");
        title.className = "palette-title";
        title.textContent = hit.title;
        const where = document.createElement("span");
        where.className = "palette-where";
        where.textContent = `${hit.space} · ${hit.path}`;
        link.append(title, where);
        item.append(link);
        item.addEventListener("mousemove", () => { if (active !== index) highlight(index); });
        return item;
      }));
      list.hidden = results.length === 0;
      input.setAttribute("aria-expanded", String(results.length > 0));
      highlight(results.length ? 0 : -1);
      status.textContent = message;
      shown = query;
    };
    const run = async () => {
      const text = input.value.trim();
      const query = key();
      if (query === asked) return;
      asked = query;
      controller?.abort();
      if (text.length < 2) { show([], text ? dialog.dataset.short : "", query); return; }
      controller = new AbortController();
      status.textContent = dialog.dataset.busy;
      try {
        const url = `${dialog.dataset.url}?q=${encodeURIComponent(text)}&space=${encodeURIComponent(scope())}`;
        const response = await fetch(url, { credentials: "same-origin", signal: controller.signal, headers: { Accept: "application/json" } });
        const data = await response.json();
        if (!response.ok) { show([], data.error || dialog.dataset.failed, query); return; }
        const n = data.results.length;
        const message = n === 0 ? dialog.dataset.empty
          : n === 1 ? dialog.dataset.foundOne : dialog.dataset.foundMany.replace("{n}", n);
        show(data.results, message, query);
      } catch (error) {
        if (error.name === "AbortError") return;
        asked = "";  // session expired, network: can retry
        show([], dialog.dataset.failed, "");
      }
    };
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 160); });
    form.addEventListener("change", event => {
      if (event.target.name !== "space") return;
      run();
      input.focus();
    });
    input.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        // Otherwise a search field clears first, and a second Escape is needed.
        event.preventDefault();
        dialog.close();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (options().length) highlight(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Enter" && !event.isComposing && !event.shiftKey) {
        // Results up to date: the chosen page. Otherwise (typing faster than the search),
        // the form goes to the full search page.
        const option = options()[active];
        if (option && shown === key()) {
          event.preventDefault();
          window.location.assign(option.querySelector("a").href);
        }
      }
    });
    const open = () => {
      if (!dialog.open) {
        opener = document.activeElement;
        dialog.showModal();
      }
      input.focus();
      input.select();
    };
    dialog.addEventListener("close", () => {
      clearTimeout(timer);
      controller?.abort();
      asked = shown;
      if (opener && document.contains(opener) && typeof opener.focus === "function") opener.focus();
      opener = null;
    });
    dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });  // backdrop
    dialog.querySelectorAll("[data-palette-close]").forEach(button => button.addEventListener("click", () => dialog.close()));
    document.querySelectorAll("[data-palette-open]").forEach(link => link.addEventListener("click", event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;  // new tab
      event.preventDefault();
      open();
    }));
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.isComposing || event.altKey) return;
      const typing = event.target.closest?.("input, textarea, select, [contenteditable]");
      const shortcut = event.key.toLowerCase() === "k" && (event.ctrlKey || event.metaKey);
      if (shortcut || (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey)) {
        event.preventDefault();
        open();
      }
    });
  };

  // --- Tabs too wide (mobile): current tab visible, faded edges if more remain ---------------
  // Scroll the bar only (not `scrollIntoView`, which also moves the page).
  const setupTabs = () => {
    document.querySelectorAll(".tabs").forEach(tabs => {
      const edges = () => {
        const max = tabs.scrollWidth - tabs.clientWidth;
        tabs.classList.toggle("more-before", max > 1 && tabs.scrollLeft > 1);
        tabs.classList.toggle("more-after", max > 1 && tabs.scrollLeft < max - 1);
      };
      const current = tabs.querySelector("[aria-current]");
      if (current && tabs.scrollWidth > tabs.clientWidth) {
        const left = current.offsetLeft - tabs.offsetLeft;
        if (left + current.offsetWidth > tabs.clientWidth) tabs.scrollLeft = left - (tabs.clientWidth - current.offsetWidth) / 2;
      }
      edges();
      tabs.addEventListener("scroll", edges, { passive: true });
      window.addEventListener("resize", edges);
    });
  };

  // --- Freshly created token: focus on "Copy", confirmation before leaving without copying -----
  const setupRevealed = () => {
    const box = document.querySelector("[data-revealed]");
    if (!box) return;
    let copied = false;
    box.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", () => { copied = true; }));
    box.querySelector("[data-copy]")?.focus();
    window.addEventListener("beforeunload", event => { if (!copied) event.preventDefault(); });
  };

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(root.dataset.theme);
    document.querySelectorAll("[data-theme-option]").forEach(button => {
      button.disabled = false;
      button.addEventListener("click", () => { applyTheme(button.dataset.themeOption); write("theme", root.dataset.theme); });
    });
    setupSidebar();
    if (narrow()) document.querySelectorAll("details.wiki-tree-panel").forEach(panel => { panel.open = false; });
    setupConfirmations();
    setupForms();
    setupDates();
    setupEditor();
    setupPreview();
    setupVocationTemplate();
    setupCopy();
    setupTabs();
    setupRevealed();
    setupTree();
    setupRelated();
    setupMenus();
    setupAssistantTest();
    setupPendingTest();
    setupTidyJournal();
    setupAccessLimits();
    setupProviderFields();
    setupRepoMode();
    setupFilePickers();
    setupToc();
    setupCrumbs();
    setupPalette();
    setupDiagrams();
    setupFirstSteps();
  });
  window.addEventListener("storage", event => { if (event.key === prefix + "theme") applyTheme(event.newValue); });
})();
