// Pure-JS session that bridges an xterm.js terminal to an agent's
// tmux WebSocket. Lives outside React so the wire protocol, listener
// wiring, and resource cleanup don't have to ride the render cycle.
// `TerminalView` mounts a session via a callback ref and tears it
// down on the matching detach.
//
// Wire protocol (mirrors `omnigent/server/routes/terminal_attach.py`):
//   - Server → client: binary frames, raw PTY bytes → `term.write`.
//   - Client → server: binary frames for keystrokes (`term.onData`);
//     text frames for JSON control messages (currently only resize).

import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { type ITheme, Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";

// Card background colors derived from the app's CSS palette.
// Light: --card: oklch(1.000 0 0) = pure white.
// Dark:  --card: oklch(0.195 0.004 240) ≈ rgb(19, 21, 23) via OKLab → sRGB.
const CARD_LIGHT = "#ffffff";
const CARD_DARK = "#131517";

/**
 * Return an xterm `ITheme` object matched to the app's light or dark palette.
 */
export function terminalTheme(isDark: boolean): ITheme {
  const bg = isDark ? CARD_DARK : CARD_LIGHT;
  return isDark
    ? {
        background: bg,
        foreground: "#e4e4e7",
        cursor: "#22d3ee",
        cursorAccent: bg,
        selectionBackground: "#22d3ee33",
        black: "#09090b",
        brightBlack: "#71717a",
      }
    : {
        background: bg,
        foreground: "#18181b",
        cursor: "#0891b2",
        cursorAccent: bg,
        selectionBackground: "#0891b233",
        black: "#18181b",
        brightBlack: "#e4e4e7",
        // CLIs that assume a dark terminal paint primary text with ANSI
        // white / bright-white. On the white card background those slots
        // must be dark tones, or the text renders white-on-white and
        // vanishes. brightWhite is the most emphasized text, so it maps to
        // the strongest (darkest) tone; white is a slightly muted gray.
        white: "#3f3f46",
        brightWhite: "#18181b",
      };
}

/**
 * Activation handler for clickable links in terminal output.
 *
 * Wired into {@link WebLinksAddon}. Suppresses the addon's default
 * navigation (which would replace the SPA — and the live terminal
 * session — with the link target) and opens the URL in a new tab
 * instead. ``noopener,noreferrer`` denies the opened page a handle
 * back to this window and strips the ``Referer`` header.
 *
 * Exported for direct unit testing; production code passes it to the
 * addon constructor rather than calling it directly.
 *
 * :param event: The DOM mouse event from the link click. Its default
 *     action (addon-driven navigation) is prevented.
 * :param uri: The URL the addon detected in the terminal output,
 *     e.g. ``"https://example.com/foo"``.
 */
export function openTerminalLink(event: MouseEvent, uri: string): void {
  event.preventDefault();
  window.open(uri, "_blank", "noopener,noreferrer");
}

/**
 * Lifecycle state of the bridge, surfaced to React for the
 * connecting / closed / error overlays.
 *
 * The ``closed`` variant carries the WebSocket close code alongside
 * the human-readable reason so consumers can distinguish deliberate
 * server closes (the 4xxx app codes) from transport-level drops —
 * see {@link isUnexpectedTerminalClose}.
 */
export type ConnectionState =
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "closed"; reason: string; code: number }
  | { kind: "error" };

/**
 * Decide whether a WebSocket close code represents a transport-level
 * drop worth auto-reconnecting, rather than a deliberate close.
 *
 * Deliberate closes — normal closure (1000), auth/policy rejections
 * (1008), and the app's own 4xxx codes (4404 terminal-not-found,
 * 4405 terminal-detached, 4500 internal error; see
 * ``omnigent/terminals/ws_bridge.py``) — mean the server decided the
 * attach should end, so re-dialing would either loop on the same
 * answer or resurrect a terminal the user intentionally left.
 *
 * Transport-shaped closes happen *to* the connection rather than
 * being decided by either end's terminal logic:
 *
 * - 1006: abnormal closure, no close frame. The classic background-tab
 *   case — the tab freezes, buffered output stalls the socket, the
 *   server's keepalive ping times out, and the browser discovers a
 *   dead TCP connection on thaw.
 * - 1001: "going away" — a server or proxy restarting.
 * - 1012 / 1013: service restart / try again later.
 *
 * Pure helper — exported for direct unit testing.
 *
 * :param code: The WebSocket close code from the ``close`` event,
 *     e.g. ``1006``.
 * :returns: ``true`` when the close is transport-shaped and a
 *     reconnect attempt is appropriate.
 */
export function isUnexpectedTerminalClose(code: number): boolean {
  return code === 1001 || code === 1006 || code === 1012 || code === 1013;
}

/** Listener for `ConnectionState` transitions. */
export type ConnectionStateListener = (state: ConnectionState) => void;

/** Listener for terminal output activity from the server. */
export type TerminalActivityListener = () => void;
/** Listener for user keyboard input sent to the terminal. */
export type TerminalInputListener = () => void;

/** Kitty Keyboard Protocol / CSI-u encoding for Shift+Enter. */
export const SHIFT_ENTER_CSI_U = "\x1b[13;2u";

/**
 * Return the terminal bytes to send for a browser key event.
 *
 * xterm.js does not currently emit Kitty Keyboard Protocol sequences for
 * Shift+Enter, so the browser attach path synthesizes the CSI-u sequence
 * for that one key combination. This mirrors native terminals that support
 * CSI-u while keeping plain Enter and modified Enter variants on xterm's
 * default path.
 *
 * :param event: Browser keyboard event from xterm's custom key handler.
 * :returns: CSI-u bytes for Shift+Enter, or ``null`` to let xterm handle
 *     the event normally.
 */
export function terminalKeyEventPayload(event: KeyboardEvent): string | null {
  if (
    event.key === "Enter" &&
    event.shiftKey &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.metaKey
  ) {
    return SHIFT_ENTER_CSI_U;
  }
  return null;
}

// Reused across keystrokes — allocating a fresh TextEncoder per keypress
// is needless churn on the input hot path.
const INPUT_ENCODER = new TextEncoder();

/**
 * How recently the user must have typed for an inbound chunk to count as
 * an echo, and the largest chunk still eligible for the synchronous paint.
 */
export const SYNC_ECHO_WINDOW_MS = 750;
export const SYNC_ECHO_MAX_BYTES = 2048;

/**
 * Decide whether an inbound PTY chunk should be painted synchronously
 * rather than queued through xterm's async ``write``.
 *
 * The public ``term.write`` defers parsing+paint to a later
 * microtask/frame, adding a frame (or more, under load) of keystroke→echo
 * latency. When the user typed within the last {@link SYNC_ECHO_WINDOW_MS}
 * and the chunk is small (≤ {@link SYNC_ECHO_MAX_BYTES} — an echo or
 * prompt redraw, not a flood), painting it in the same task makes typing
 * feel immediate. Large chunks stay on the async path so an output flood
 * can't monopolize the main thread. Mirrors openui's terminal input fast
 * path.
 *
 * Pure helper — exported for direct unit testing.
 *
 * :param byteLength: Size of the inbound chunk in bytes.
 * :param msSinceLastInput: Milliseconds since the last user keystroke.
 * :returns: ``true`` to take the synchronous echo path.
 */
export function shouldEchoSynchronously(byteLength: number, msSinceLastInput: number): boolean {
  return msSinceLastInput < SYNC_ECHO_WINDOW_MS && byteLength <= SYNC_ECHO_MAX_BYTES;
}

/**
 * Structural view of xterm's internal core, used only to reach the
 * synchronous ``writeSync`` method that the public types don't expose
 * (see {@link TerminalSession.writeOutput}).
 */
// eslint-disable-next-line no-underscore-dangle
type TerminalCore = {
  _core?: { writeSync?: (data: Uint8Array, maxSubsequentCalls?: number) => void };
};

/**
 * Structural view onto xterm's render service, used only to read the
 * computed cell size for letterbox dot-tiling (see
 * {@link TerminalSession.cellSize}). All optional — every access is guarded.
 */
// eslint-disable-next-line no-underscore-dangle
type TerminalCellDims = {
  _core?: {
    _renderService?: { dimensions?: { css?: { cell?: { width: number; height: number } } } };
  };
};

/**
 * Load the WebGL renderer onto *term*, returning the addon or ``null``
 * when WebGL is unavailable and the DOM renderer stays in use.
 *
 * xterm's default DOM renderer rebuilds spans on every paint, which
 * dominates the main thread on heavy output (large ``cat``, build logs,
 * a redrawing TUI); the WebGL renderer rasterizes glyphs on the GPU and
 * is dramatically faster for those bursts. Loaded *after*
 * {@link Terminal.open} because it needs the mounted ``<canvas>``. Both
 * the no-GPU and context-lost paths fall back to the DOM renderer rather
 * than freezing the canvas — see the inline comments below.
 */
export function loadWebglRenderer(term: Terminal): WebglAddon | null {
  let addon: WebglAddon;
  try {
    addon = new WebglAddon();
  } catch {
    return null;
  }
  // Dispose on context loss so xterm reverts to the DOM renderer; a
  // disposed WebGL addon left attached would freeze on its last frame.
  addon.onContextLoss(() => addon.dispose());
  try {
    term.loadAddon(addon);
  } catch {
    // WebGL unsupported in this environment (no GPU context, jsdom).
    // The DOM renderer stays active; correctness is unaffected.
    addon.dispose();
    return null;
  }
  return addon;
}

/**
 * Populate the clipboard from a terminal text selection on a browser
 * ``copy`` event.
 *
 * The attached tmux session runs with ``mouse on``, so a plain click-drag
 * is captured by tmux for its own copy-mode and never becomes a browser
 * selection; the user makes a selection with Shift-drag (non-Mac) or
 * ⌥-drag (Mac, via ``macOptionClickForcesSelection``). xterm renders that
 * selection in its own layer rather than a DOM range, so the browser's
 * default copy of it is unreliable — we feed ``term.getSelection()`` into
 * the event's ``clipboardData`` ourselves. ``getSelection()`` already
 * rejoins soft-wrapped rows, so a paragraph the terminal wrapped across
 * several rows copies back as one logical line.
 *
 * We never remap Ctrl+C — in a terminal it must stay SIGINT — so on
 * Linux/Windows this fires via right-click → Copy (and Edit → Copy); on
 * macOS ⌘C also dispatches a browser ``copy`` event.
 *
 * Pure helper — exported for direct unit testing; production code wires it
 * to a container ``copy`` listener rather than calling it directly.
 *
 * :param event: The browser ``copy`` event.
 * :param selection: The current terminal selection text ("" if none).
 * :returns: ``true`` if the clipboard was populated, ``false`` when there
 *     was no selection to copy (the event is left untouched so the
 *     browser's default copy behavior still applies elsewhere).
 */
export function applyTerminalCopy(
  event: Pick<ClipboardEvent, "clipboardData" | "preventDefault">,
  selection: string,
): boolean {
  if (!selection) return false;
  event.clipboardData?.setData("text/plain", selection);
  event.preventDefault();
  return true;
}

/**
 * One xterm ↔ tmux WebSocket bridge tied to a single DOM container.
 *
 * The constructor performs all the setup synchronously — open the
 * terminal on the container, open the WebSocket, wire up listeners,
 * attach a ResizeObserver. {@link dispose} tears them all down in
 * the same order callers expect: abort listeners first (so the
 * close event doesn't fire stale state into a remounted view),
 * disconnect the observer, dispose the xterm data subscription,
 * close the WS, dispose the terminal.
 */
export class TerminalSession {
  private readonly term: Terminal;
  private readonly fit: FitAddon;
  /** The DOM node xterm is mounted under; its background paints the
   * letterbox margin when the grid is pinned smaller than the container. */
  private readonly container: HTMLElement;
  /** WebGL renderer addon, or ``null`` when WebGL is unavailable. */
  private readonly webgl: WebglAddon | null;
  private readonly ws: WebSocket;
  private readonly listenerCtl: AbortController;
  private readonly resizeObserver: ResizeObserver;
  private readonly dataDispose: { dispose: () => void };
  /** ``performance.now()`` of the last keystroke; gates the echo fast path. */
  private lastUserInputAt = 0;
  /** Guards {@link dispose} so calling it twice is a safe no-op. */
  private disposed = false;
  /**
   * True once the server has pushed an authoritative (effective) grid size.
   * The Windows ConPTY bridge sends a ``resize`` control frame carrying the
   * smallest-wins size shared across all attached clients; from then on the
   * server owns the grid dimensions, so a larger client renders the shared
   * smaller grid (clean empty margin) instead of fitting its big container
   * and showing stale cells outside the active region. The tmux bridge never
   * sends these frames, so this stays false there and fit-to-container holds.
   */
  private serverPinnedSize = false;

  /**
   * Construct, attach to the DOM, and open the WebSocket.
   *
   * :param container: DOM node to mount the xterm Terminal under.
   * :param url: Fully-qualified ``ws(s)://`` URL for the
   *     ``.../resources/terminals/{id}/attach`` endpoint.
   * :param onState: Called with each state transition so React can
   *     render the connecting / closed / error overlay. Invoked
   *     synchronously from WS event handlers.
   * :param onActivity: Called whenever PTY output arrives from the
   *     server. This is a best-effort UI activity signal, not a shell
   *     job-state oracle.
   * :param onInput: Called when user input is sent to the terminal.
   */
  constructor(
    container: HTMLElement,
    url: string,
    onState: ConnectionStateListener,
    isDark = false,
    onActivity?: TerminalActivityListener,
    onInput?: TerminalInputListener,
  ) {
    this.term = new Terminal({
      // Match the system mono stack at the configured base size. The
      // xterm.js defaults (15px, no theme) feel out of place inside the
      // app chrome.
      fontFamily:
        "'Geist Mono Variable', ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace",
      fontSize: 13,
      scrollback: 20000,
      cursorBlink: true,
      theme: terminalTheme(isDark),
      // 256-color indices (e.g. Claude Code's 38;5;231 white) can't be
      // remapped via ITheme (slots 0-15 only), so they vanish on the
      // light theme's white card. This WCAG AA contrast floor nudges a
      // cell's foreground luminance only when it lacks contrast against
      // its actual background.
      minimumContrastRatio: 4.5,
      // The attached tmux session runs with `mouse on` (terminal.py) so
      // the wheel pages through scrollback. The downside is tmux then
      // captures every mouse drag for its own copy-mode, so a plain
      // click-drag never produces a browser text selection — the user
      // can't select-and-copy. xterm's escape hatch is
      // `shouldForceSelection`: on non-Mac it honors Shift-drag for
      // free, but on Mac it forces a native selection only when
      // `macOptionClickForcesSelection` is enabled AND the user holds
      // Option. Without this flag Mac users have no way to select at
      // all. Enabling it lets ⌥-drag select, then ⌘-C copies.
      macOptionClickForcesSelection: true,
      // Opt into xterm's proposed APIs, matching openui's terminal setup.
      allowProposedApi: true,
    });
    this.container = container;
    this.fit = new FitAddon();
    this.term.loadAddon(this.fit);
    // Turn bare URLs in terminal output into clickable links. Without
    // this addon xterm renders URLs as plain text.
    this.term.loadAddon(new WebLinksAddon(openTerminalLink));
    this.term.open(container);
    // Load the GPU renderer after open() (it needs the mounted canvas).
    // Falls back to the DOM renderer when WebGL is unavailable.
    this.webgl = loadWebglRenderer(this.term);
    try {
      this.fit.fit();
    } catch (err) {
      console.warn("[terminal-attach] initial fit failed, falling back to 80x24", err);
      this.term.resize(80, 24);
    }

    this.ws = new WebSocket(url);
    // Default is Blob, which forces an async read per chunk. ArrayBuffer
    // keeps the path synchronous and matches xterm.js's preferred input.
    this.ws.binaryType = "arraybuffer";

    // AbortController-scoped listeners so the cleanup's ws.close()
    // can't fire stale `close`/`error` events into the next mount —
    // under React StrictMode this otherwise flickers a "Bridge
    // closed" overlay on top of the freshly-connecting terminal.
    this.listenerCtl = new AbortController();
    const { signal } = this.listenerCtl;

    // Make the browser copy gesture (right-click → Copy and Edit → Copy on
    // every platform, ⌘C on macOS) yield the terminal selection as text.
    // Without this, a Shift/⌥-drag selection has no working copy path on
    // Linux/Windows — Ctrl+C is SIGINT, and xterm's selection layer isn't a
    // DOM range the browser copies on its own. Capture phase + the shared
    // abort signal so `dispose()` removes it for free. Ctrl+C is never
    // remapped (see {@link applyTerminalCopy}).
    container.addEventListener(
      "copy",
      // getSelection() returns "" when nothing is selected, which
      // applyTerminalCopy treats as a no-op — no hasSelection() guard needed.
      (e) => applyTerminalCopy(e, this.term.getSelection()),
      { capture: true, signal },
    );

    this.ws.addEventListener(
      "open",
      () => {
        // Send the size first so tmux re-renders at the right
        // dimensions before the user sees the default 80×24 followed
        // by a reflow.
        this.sendResize();
        this.term.focus();
        onState({ kind: "connected" });
      },
      { signal },
    );

    // Throttle activity notifications so rapid output (e.g. `yes`, large
    // `cat`) doesn't re-arm the 1.5 s idle timer on every WS frame.
    let lastActivityTs = 0;
    this.ws.addEventListener(
      "message",
      (ev) => {
        if (ev.data instanceof ArrayBuffer) {
          const bytes = new Uint8Array(ev.data);
          this.writeOutput(bytes);
          const now = performance.now();
          if (now - lastActivityTs > 300) {
            lastActivityTs = now;
            onActivity?.();
          }
          return;
        }
        // Text frames are JSON control messages. The Windows ConPTY bridge
        // sends ``{type:"resize",cols,rows}`` to pin this client to the shared
        // smallest-wins grid size (see handleControlFrame). Never written to
        // the terminal as output.
        if (typeof ev.data === "string") {
          this.handleControlFrame(ev.data);
        }
      },
      { signal },
    );

    this.ws.addEventListener(
      "close",
      (ev) => {
        onState({ kind: "closed", reason: ev.reason || `code ${ev.code}`, code: ev.code });
      },
      { signal },
    );

    this.ws.addEventListener(
      "error",
      () => {
        onState({ kind: "error" });
      },
      { signal },
    );

    this.dataDispose = this.term.onData((d) => {
      onInput?.();
      // Stamp the keystroke so the next inbound chunk can take the
      // synchronous echo path; stamp before the readyState guard so a
      // momentary WS hiccup doesn't disarm the fast path.
      this.lastUserInputAt = performance.now();
      if (this.ws.readyState !== WebSocket.OPEN) return;
      this.ws.send(INPUT_ENCODER.encode(d));
    });

    this.term.attachCustomKeyEventHandler((e) => {
      const payload = terminalKeyEventPayload(e);
      if (payload === null) return true;
      // xterm invokes this handler for keydown, keypress, and keyup.
      // Suppress all three so xterm cannot also send a bare Enter; emit
      // the CSI-u sequence once, on keydown.
      if (e.type === "keydown") {
        e.preventDefault();
        onInput?.();
        this.lastUserInputAt = performance.now();
        if (this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(INPUT_ENCODER.encode(payload));
        }
      }
      return false;
    });

    // ResizeObserver fires on any layout-affecting change (window
    // resize, font load, CSS class change). tmux deduplicates same-
    // size events server-side, so no throttle needed here.
    this.resizeObserver = new ResizeObserver(() => this.sendResize());
    this.resizeObserver.observe(container);
  }

  /**
   * Update the terminal's color theme without reconnecting the WebSocket.
   * Safe to call at any point after construction.
   */
  setTheme(isDark: boolean): void {
    this.term.options.theme = terminalTheme(isDark);
    // Repaint the letterbox margin in the new theme's colors.
    this.updateLetterbox();
  }

  /**
   * Return xterm's rendered cell size in CSS px, or ``null`` if unknown.
   *
   * Prefers the renderer's own dimensions; falls back to measuring the
   * mounted grid element. Used to tile the letterbox dot pattern so its
   * dots line up with the (hidden) character grid.
   */
  private cellSize(): { width: number; height: number } | null {
    try {
      // eslint-disable-next-line no-underscore-dangle
      const dims = (this.term as unknown as TerminalCellDims)._core?._renderService?.dimensions?.css
        ?.cell;
      if (dims && dims.width > 0 && dims.height > 0) {
        return { width: dims.width, height: dims.height };
      }
    } catch {
      /* fall through to measurement */
    }
    const el = this.term.element;
    if (el && this.term.cols > 0 && this.term.rows > 0) {
      const width = el.clientWidth / this.term.cols;
      const height = el.clientHeight / this.term.rows;
      if (width > 0 && height > 0) return { width, height };
    }
    return null;
  }

  /**
   * Paint (or clear) the letterbox margin behind the grid.
   *
   * When the server has pinned this client to a shared smallest-wins grid
   * smaller than its container, the leftover L-shaped margin would show
   * stale pixels. Fill it with a dim per-cell dot pattern over the terminal
   * background — the web analog of tmux filling the inactive region of a
   * larger client with ``·``. The opaque ``.xterm`` element covers the
   * active grid, so dots show only in the margin. No-op (and clears any
   * prior fill) when the server hasn't pinned a size.
   */
  private updateLetterbox(): void {
    const style = this.container.style;
    if (!this.serverPinnedSize) {
      style.backgroundColor = "";
      style.backgroundImage = "";
      return;
    }
    const theme = this.term.options.theme ?? {};
    const bg = typeof theme.background === "string" ? theme.background : "#000000";
    const fgRaw = typeof theme.foreground === "string" ? theme.foreground : "#808080";
    // Dim the dots to ~33% so the inactive margin reads as secondary chrome,
    // not content. 8-digit hex alpha only when the value is a 6-digit hex.
    const dot = /^#[0-9a-f]{6}$/i.test(fgRaw) ? `${fgRaw}55` : fgRaw;
    style.backgroundColor = bg;
    const cell = this.cellSize();
    if (cell === null) {
      // Couldn't measure cells — at least keep the margin a clean bg block.
      style.backgroundImage = "";
      return;
    }
    style.backgroundImage = `radial-gradient(circle at center, ${dot} 1px, transparent 1.5px)`;
    style.backgroundSize = `${cell.width}px ${cell.height}px`;
    style.backgroundPosition = "0 0";
    style.backgroundRepeat = "repeat";
  }

  /**
   * Tear down the bridge. Order matters: abort listeners FIRST so
   * the cleanup's ``ws.close()`` can't fire a stale ``close``
   * event into the next mount.
   *
   * Idempotent: the view disposes the outgoing session explicitly on
   * every re-dial (React 18 ignores callback-ref cleanups), and a
   * future React upgrade would have the ref cleanup call this again.
   */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.listenerCtl.abort();
    this.resizeObserver.disconnect();
    this.dataDispose.dispose();
    try {
      this.ws.close();
    } catch {
      /* noop */
    }
    // Dispose the WebGL renderer before the terminal so its canvas and
    // GL context are released while the terminal still owns them.
    this.webgl?.dispose();
    this.term.dispose();
  }

  /**
   * Write inbound PTY bytes to the terminal, taking the synchronous echo
   * fast path for small chunks that arrive right after a keystroke (see
   * {@link shouldEchoSynchronously}).
   *
   * ``writeSync`` is an internal xterm method not in the public typings,
   * so it's feature-detected and wrapped in try/catch: any failure — or a
   * future xterm that drops it — falls back to the async public ``write``.
   * Correctness never depends on the private API; it only shaves a frame
   * off the echo when present.
   */
  private writeOutput(bytes: Uint8Array): void {
    if (shouldEchoSynchronously(bytes.length, performance.now() - this.lastUserInputAt)) {
      // eslint-disable-next-line no-underscore-dangle
      const core = (this.term as unknown as TerminalCore)._core;
      if (typeof core?.writeSync === "function") {
        try {
          core.writeSync(bytes, 1);
          return;
        } catch {
          /* fall through to the async public write */
        }
      }
    }
    this.term.write(bytes);
  }

  /**
   * Apply a server-pushed effective grid size (Windows ConPTY smallest-wins).
   *
   * Pins the xterm grid to the shared ``cols``×``rows`` and marks the server
   * authoritative, so subsequent container changes report their size but no
   * longer resize the grid locally — a larger client letterboxes the shared
   * smaller grid instead of showing stale cells (the multi-client artifacts).
   *
   * :param text: Raw text frame; a JSON ``{type:"resize",cols,rows}`` message.
   */
  private handleControlFrame(text: string): void {
    let msg: unknown;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (typeof msg !== "object" || msg === null) return;
    const m = msg as { type?: unknown; cols?: unknown; rows?: unknown };
    if (m.type !== "resize") return;
    const cols = Number(m.cols);
    const rows = Number(m.rows);
    if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1) return;
    const firstPin = !this.serverPinnedSize;
    this.serverPinnedSize = true;
    if (this.term.cols === cols && this.term.rows === rows) {
      // Already at the shared size, but if this is the first pin the
      // letterbox margin still needs painting.
      if (firstPin) this.updateLetterbox();
      return;
    }
    try {
      this.term.resize(cols, rows);
    } catch {
      /* noop */
    }
    this.updateLetterbox();
  }

  private sendResize(): void {
    if (this.ws.readyState !== WebSocket.OPEN) return;
    // Measure the container WITHOUT committing the grid to it: with several
    // browsers on one ConPTY the server negotiates a shared smallest-wins size
    // and pushes it back (handleControlFrame). We always REPORT our capacity so
    // that negotiation sees our true size, but only render at it ourselves
    // until the server pins an authoritative size. The tmux bridge never pins,
    // so there fit-to-container always holds (proposeDimensions == old fit()).
    const dims = this.fit.proposeDimensions();
    if (!dims || !dims.cols || !dims.rows) return;
    if (!this.serverPinnedSize && (this.term.cols !== dims.cols || this.term.rows !== dims.rows)) {
      try {
        this.term.resize(dims.cols, dims.rows);
      } catch {
        return;
      }
    }
    this.ws.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
  }
}
