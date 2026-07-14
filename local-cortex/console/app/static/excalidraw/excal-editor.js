/* ===========================================================================
 * Kaidera OS Console — LIVE Excalidraw editor glue (E007 workspace).
 *
 * Mounts the vendored @excalidraw/excalidraw React canvas into the center file
 * pane for `.excalidraw` / `.excalidraw.md` files, exactly like Obsidian's
 * view+edit model: the user views AND draws on a real canvas; an explicit Save
 * persists; unsaved edits live only in the canvas and are LOST on close.
 *
 * It is harness-agnostic vanilla JS (no build step). It is loaded once from
 * console.html via a <script defer> tag; the per-file mount is kicked off by an
 * inline bootstrap in _editor.html (window.ExcalEditor.mount({...})) every time
 * the HTMX editor body swaps in an excalidraw file.
 *
 * DATA FLOW (no new server route — uses the EXISTING workspace routes):
 *   - The server hands us the file's RAW content (the .excalidraw(.md) text) via
 *     the _editor.html bootstrap. We parse the scene CLIENT-SIDE:
 *       · Obsidian ```compressed-json``` block  -> LZString.decompressFromBase64
 *       · uncompressed ```json``` block          -> JSON.parse
 *       · raw .excalidraw                        -> JSON.parse(whole file)
 *   - We split the Obsidian markdown wrapper into the PREFIX (front-matter +
 *     `# Excalidraw Data` + `## Text Elements`, everything ABOVE `## Drawing`)
 *     so Save can replace ONLY the drawing block — front-matter/text preserved,
 *     matching workspace.py:_build_obsidian_markdown in shape.
 *   - SAVE serialises the current scene (Excalidraw's getSceneElements +
 *     getAppState + getFiles), re-wraps it into the Obsidian markdown (native
 *     compressed-json so the file looks exactly like Obsidian's), and POSTs the
 *     full text to the EXISTING  POST /workspace/{key}/file?path=...  route as the
 *     `content` field (urlencoded) — the same write that text files use, behind
 *     the same _safe_target repo_root security gate.
 *
 * The Excalidraw React state IS the single source of truth for unsaved edits; we
 * never write to disk until Save. Closing the pane discards the React tree, so
 * unsaved edits vanish (re-open re-reads the on-disk file) — the Obsidian model.
 * ========================================================================= */
(function () {
  "use strict";

  // Point Excalidraw's webpack public path at our vendored asset dir BEFORE the
  // library loads its fonts / vendor chunk, so it self-hosts fully offline (no
  // unpkg.com fallback). Trailing slash required by webpack.
  window.EXCALIDRAW_ASSET_PATH = "/static/excalidraw/";

  var NS = {};

  // ---- markdown wrapper helpers (mirror workspace.py) --------------------

  // Line-anchored `## Drawing` heading — the boundary between the human note and
  // the machine-owned scene block. Matches workspace.py:_split_excalidraw_wrapper.
  var DRAWING_RE = /^[ \t]*##[ \t]+Drawing[ \t]*$/m;

  // Default Obsidian front-matter scaffold for a brand-new / raw file that has no
  // prefix yet (kept in sync with workspace.py:_build_obsidian_markdown).
  var DEFAULT_PREFIX =
    "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n" +
    "==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this " +
    "document. ⚠==\n\n# Excalidraw Data\n\n## Text Elements\n%%\n";

  // Split an .excalidraw.md body into [prefixAboveDrawing, drawingSectionOrNull].
  function splitWrapper(content) {
    if (!content) return ["", null];
    var m = DRAWING_RE.exec(content);
    if (!m) return [content, null];
    return [content.slice(0, m.index), content.slice(m.index)];
  }

  // Decode the scene object from a raw .excalidraw(.md) file body, trying the
  // same source order as workspace.py:parse_excalidraw. Returns a scene object
  // {elements, appState, files, ...} or null.
  function parseScene(content) {
    if (!content) return null;
    var candidates = [];

    // 1) Obsidian compressed-json block (authoritative current scene).
    var cm = content.match(/```compressed-json\s*([\s\S]*?)\s*```/);
    if (cm && window.LZString) {
      var packed = cm[1].replace(/\s+/g, "");
      try {
        var dec = window.LZString.decompressFromBase64(packed);
        if (dec) candidates.push(dec);
      } catch (e) { /* fall through */ }
    }
    // 2) uncompressed ```json``` scene block(s).
    var jblocks = content.match(/```json\s*\{[\s\S]*?\}\s*```/g) || [];
    jblocks.forEach(function (blk) {
      var inner = blk.replace(/^```json\s*/, "").replace(/\s*```$/, "");
      candidates.push(inner);
    });
    // 3) raw .excalidraw — the whole file is the object.
    if (content.replace(/^\s+/, "").charAt(0) === "{") candidates.push(content);
    // 4) widest brace-balanced slice (last resort).
    var first = content.indexOf("{");
    var last = content.lastIndexOf("}");
    if (first !== -1 && last > first) candidates.push(content.slice(first, last + 1));

    for (var i = 0; i < candidates.length; i++) {
      try {
        var obj = JSON.parse(candidates[i]);
        if (obj && typeof obj === "object" && Array.isArray(obj.elements)) {
          return obj;
        }
      } catch (e) { /* try next */ }
    }
    return null;
  }

  // Build the file text to SAVE.
  //   - .excalidraw.md -> prefix (preserved) + `## Drawing` + compressed-json scene
  //     (Obsidian-native, so the saved file is identical in shape to Obsidian's;
  //     the compressed payload is cross-readable by workspace.py's lzstring).
  //   - raw .excalidraw -> the bare pretty scene JSON.
  function serializeFile(spec, scene) {
    var sceneJson = JSON.stringify(scene, null, 2);
    if (!spec.isMarkdown) {
      return sceneJson; // raw .excalidraw — whole file is the scene
    }
    // Derive the verbatim prefix (everything above `## Drawing`) from the
    // current on-disk text so front-matter + Text Elements are preserved; fall
    // back to the Obsidian scaffold for a brand-new / driver-less file.
    var split = splitWrapper(spec.raw || "");
    var prefix = (split[0] && split[0].trim()) ? split[0] : DEFAULT_PREFIX;
    if (prefix.charAt(prefix.length - 1) !== "\n") prefix += "\n";

    // Obsidian-native: compress the *minified* scene JSON to base64 and wrap it
    // in a ```compressed-json``` block. (Obsidian also reads uncompressed ```json```
    // — see workspace.py — but compressed keeps perfect parity with Obsidian.)
    if (window.LZString) {
      var packed = window.LZString.compressToBase64(JSON.stringify(scene));
      return prefix + "## Drawing\n```compressed-json\n" + packed + "\n```\n%%";
    }
    // No compressor available -> uncompressed JSON block (still Obsidian-valid).
    return prefix + "## Drawing\n```json\n" + sceneJson + "\n```\n%%";
  }

  // Canonical empty scene (used if the file has no decodable drawing).
  function emptyScene() {
    return {
      type: "excalidraw",
      version: 2,
      source: "https://excalidraw.com",
      elements: [],
      appState: { gridSize: null, viewBackgroundColor: "#ffffff" },
      files: {},
    };
  }

  // Pull a clean, persistable scene out of the live Excalidraw API.
  function snapshotScene(api, base) {
    var elements = api.getSceneElements ? api.getSceneElements() : [];
    var appState = api.getAppState ? api.getAppState() : {};
    var files = api.getFiles ? api.getFiles() : {};
    // Strip transient UI-only appState keys Excalidraw itself excludes from
    // exports (collaborators is a Map and not JSON-serialisable; cursors/toasts
    // are session noise). Keep the rest so view background / grid persist.
    var keep = {};
    Object.keys(appState || {}).forEach(function (k) {
      if (k === "collaborators" || k === "cursorButton" || k === "toast") return;
      keep[k] = appState[k];
    });
    return {
      type: "excalidraw",
      version: (base && base.version) || 2,
      source: (base && base.source) || "https://excalidraw.com",
      elements: elements || [],
      appState: keep,
      files: files || {},
    };
  }

  // ---- mount / lifecycle --------------------------------------------------

  // Tear down any previously-mounted canvas (HTMX swaps the body wholesale, but
  // the React root + listeners are ours to clean up so we don't leak roots).
  function teardown() {
    if (NS.cleanupBeforeUnload) {
      window.removeEventListener("beforeunload", NS.cleanupBeforeUnload);
      NS.cleanupBeforeUnload = null;
    }
    if (NS.root && NS.root.unmount) {
      try { NS.root.unmount(); } catch (e) { /* ignore */ }
    }
    NS.root = null;
    NS.api = null;
    NS.spec = null;
    NS.dirty = false;
  }

  // Reflect dirty state in the header pip.
  function setDirty(on) {
    NS.dirty = !!on;
    var pip = document.getElementById("ed-dirty");
    if (pip) pip.hidden = !on;
  }

  // Show a transient status message in the footer slot.
  function status(msg, kind) {
    var slot = document.getElementById("xc-status");
    if (!slot) return;
    slot.textContent = msg || "";
    slot.className = "xc-status" + (kind ? " xc-status--" + kind : "");
  }

  // Public: mount the live canvas. Called from the _editor.html bootstrap each
  // time the excalidraw editor body swaps in.
  //   spec = { key, path, isMarkdown, raw }
  // (the wrapper PREFIX is derived from `raw` at save time via splitWrapper.)
  function mount(spec) {
    teardown();
    var host = document.getElementById("xc-canvas-host");
    if (!host) return;

    if (!window.React || !window.ReactDOM || !window.ExcalidrawLib) {
      host.innerHTML =
        '<div class="xc-load-err">Excalidraw library failed to load. ' +
        "Use the source view to edit this file.</div>";
      return;
    }

    NS.spec = spec;
    var initialScene = parseScene(spec.raw) || emptyScene();
    NS.baseScene = initialScene;

    var React = window.React;
    var Excalidraw = window.ExcalidrawLib.Excalidraw;
    var h = React.createElement;

    // The Excalidraw component (controlled via its imperative API ref).
    function App() {
      return h(
        "div",
        { className: "xc-fill" },
        h(Excalidraw, {
          // hand us the imperative API for save/export
          excalidrawAPI: function (api) { NS.api = api; },
          // seed the canvas with the file's scene
          initialData: {
            elements: initialScene.elements || [],
            appState: Object.assign(
              { viewBackgroundColor: "#ffffff" },
              initialScene.appState || {},
              // never restore a stale collaborators map / readonly flag
              { collaborators: undefined, viewModeEnabled: false }
            ),
            files: initialScene.files || {},
            scrollToContent: true,
          },
          langCode: "en",
          // any real change on the canvas marks the buffer dirty
          onChange: function () {
            if (!NS.suppressChange) setDirty(true);
          },
          // keep the library's own menus minimal — we own Save/Close in the pane
          UIOptions: {
            canvasActions: {
              loadScene: false,
              saveToActiveFile: false,
              export: false,
              saveAsImage: true,
            },
          },
        })
      );
    }

    NS.root = window.ReactDOM.createRoot(host);
    // Suppress the initial onChange storm (mount fires onChange) so we start clean.
    NS.suppressChange = true;
    NS.root.render(h(App));
    setDirty(false);
    setTimeout(function () { NS.suppressChange = false; }, 350);

    // Warn before unloading the whole page with unsaved canvas edits.
    NS.cleanupBeforeUnload = function (e) {
      if (NS.dirty) { e.preventDefault(); e.returnValue = ""; return ""; }
    };
    window.addEventListener("beforeunload", NS.cleanupBeforeUnload);
  }

  // Public: SAVE the current canvas to disk via the existing write route.
  function save() {
    if (!NS.api || !NS.spec) return;
    var btn = document.getElementById("xc-save");
    if (btn) btn.setAttribute("disabled", "disabled");
    status("Saving...", null);

    var scene = snapshotScene(NS.api, NS.baseScene);
    var body;
    try {
      body = serializeFile(NS.spec, scene);
    } catch (e) {
      status("Could not serialise the drawing.", "err");
      if (btn) btn.removeAttribute("disabled");
      return;
    }

    var url = "/workspace/" + encodeURIComponent(NS.spec.key) +
      "/file?path=" + encodeURIComponent(NS.spec.path);
    var form = new URLSearchParams();
    form.set("content", body);

    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    })
      .then(function (resp) {
        return resp.text().then(function (text) {
          return { ok: resp.ok, status: resp.status, text: text };
        });
      })
      .then(function (r) {
        if (btn) btn.removeAttribute("disabled");
        if (r.ok) {
          setDirty(false);
          // Refresh the col-4 tree (a brand-new file should appear), matching the
          // text-editor's ws-saved trigger.
          if (window.htmx) {
            var dock = document.getElementById("ws-dock");
            var dk = dock && dock.getAttribute("data-ws-key");
            if (dk) {
              window.htmx.ajax("GET",
                "/workspace/" + encodeURIComponent(dk) + "/tree?path=",
                { target: "#ws-tree", swap: "innerHTML" });
            }
          }
          status("Saved · " + (scene.elements.length) + " element" +
            (scene.elements.length === 1 ? "" : "s"), "ok");
          // update the new on-disk baseline so a later re-open reflects reality
          NS.spec.raw = body;
        } else {
          status("Save failed (" + r.status + ")", "err");
        }
      })
      .catch(function () {
        if (btn) btn.removeAttribute("disabled");
        status("Save failed — network error", "err");
      });
  }

  // Public: is the canvas dirty? (used by close/Esc guards in console.html)
  function isDirty() { return !!NS.dirty; }

  // Public: discard + tear down (called when the pane closes).
  function dispose() { teardown(); }

  window.ExcalEditor = {
    mount: mount,
    save: save,
    isDirty: isDirty,
    dispose: dispose,
    // Debug/test accessor: the live Excalidraw imperative API (for QA harnesses
    // to drive updateScene / inspect elements). Not used by the UI.
    _api: function () { return NS.api; },
    _setDirty: function (on) { setDirty(on); },
  };

  // ---- integrate with the existing console shell (NO console.html edits) ----
  //
  // The pane's close (✕ / Esc → closeWsEditor) and tree-file open (openWsFile)
  // are defined in console.html, which we must NOT touch. To enforce the Obsidian
  // "unsaved = lost, but confirm first" model, we WRAP those globals here (once,
  // when this glue loads) so a dirty canvas prompts before it's discarded and the
  // React root is always unmounted on close/switch. If a global isn't present we
  // skip silently (defensive).
  function wrapShellGlobals() {
    if (window.__excalWrapped) return;
    window.__excalWrapped = true;

    function confirmDiscardIfDirty() {
      if (NS.dirty) {
        return window.confirm("Discard unsaved changes to this drawing?");
      }
      return true;
    }

    if (typeof window.closeWsEditor === "function") {
      var origClose = window.closeWsEditor;
      window.closeWsEditor = function () {
        // Only guard when a live canvas is actually mounted + dirty.
        if (NS.root && NS.dirty && !confirmDiscardIfDirty()) return;
        if (NS.root) dispose(); // unmount React + drop unsaved edits
        return origClose.apply(this, arguments);
      };
    }

    if (typeof window.openWsFile === "function") {
      var origOpen = window.openWsFile;
      window.openWsFile = function () {
        // Switching to another file would swap the body out from under a dirty
        // canvas — confirm + dispose first (same discard semantics as close).
        if (NS.root && NS.dirty && !confirmDiscardIfDirty()) return;
        if (NS.root) dispose();
        return origOpen.apply(this, arguments);
      };
    }
  }
  wrapShellGlobals();
})();
