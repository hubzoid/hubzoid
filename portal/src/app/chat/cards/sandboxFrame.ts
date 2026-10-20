// Hubzoid's stand-in for the `safe-content-frame` package (Vite aliases the
// package name to this file; see vite.config.ts). assistant-ui renders MCP App
// cards through that package, which loads its relay page from a vendor host
// (`scf.auiusercontent.com`). A card's content must never go there, so this
// module keeps the same interface and points at the deployment's own sandbox
// origin instead: on loopback the other of `localhost` and `127.0.0.1` (the
// browser keeps their cookies apart), when hosted HUBZOID_APPS_SANDBOX_ORIGIN.
//
// The relay page (`hubzoid/apps/sandbox_frame.html`) runs on that origin with a
// CSP that forbids any network request, and puts the card's HTML one level
// down in an `allow-scripts` frame with an opaque origin.

export type SandboxOption = string;

export type RenderedFrame = {
  iframe: HTMLIFrameElement;
  origin: string;
  sendMessage: (data: unknown, transfer?: Transferable[]) => void;
  fullyLoadedPromiseWithTimeout: (ms: number) => Promise<void>;
  dispose: () => void;
};

type LoadErrorCode = "shim-unavailable" | "shim-error" | "render-timeout";
const CODES: LoadErrorCode[] = ["shim-unavailable", "shim-error", "render-timeout"];

function loadError(code: LoadErrorCode, message: string): Error {
  return Object.assign(new Error(message), { code });
}

export function isShimLoadError(error: unknown): error is Error & { code: LoadErrorCode } {
  return error instanceof Error && CODES.includes((error as { code?: LoadErrorCode }).code as LoadErrorCode);
}

type Sandbox = { origin: string; frame: string };
let sandbox: Promise<Sandbox> | null = null;
let apiBase = "";

/** Where this page's portal API lives (an agent's `api_base` in a gateway). */
export function setCardApiBase(base: string) {
  if (base !== apiBase) {
    apiBase = base;
    sandbox = null;
  }
}

function sandboxOrigin(): Promise<Sandbox> {
  if (!sandbox) {
    const url = `${apiBase}/portal/api/apps/sandbox?parent=${encodeURIComponent(location.origin)}`;
    sandbox = fetch(url, { credentials: "include" }).then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw loadError("shim-unavailable", body?.detail?.message || "Cards are not available here.");
      return body as Sandbox;
    });
    sandbox.catch(() => {
      sandbox = null;
    });
  }
  return sandbox;
}

export class SafeContentFrame {
  constructor(_product: string, _options: { sandbox?: SandboxOption[] } = {}) {}

  renderHtml(html: string, container: HTMLElement): Promise<RenderedFrame> {
    return this.render(new TextEncoder().encode(html), "text/html; charset=utf-8", container);
  }

  renderRaw(content: string | Uint8Array, mimeType: string, container: HTMLElement): Promise<RenderedFrame> {
    return this.render(typeof content === "string" ? new TextEncoder().encode(content) : content, mimeType, container);
  }

  async render(content: Uint8Array, mimeType: string, container: HTMLElement): Promise<RenderedFrame> {
    const box = await sandboxOrigin();
    const frameOrigin = new URL(box.origin).origin;
    if (frameOrigin === location.origin) throw loadError("shim-unavailable", "The card sandbox must be another origin.");
    const iframe = document.createElement("iframe");
    // The relay page needs its own (sandbox) origin to talk to this page; the
    // card itself runs inside it without one.
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
    iframe.setAttribute("referrerpolicy", "no-referrer");
    iframe.setAttribute("title", "Card");
    iframe.style.cssText = "border:none;width:100%;height:100%;display:block";
    container.appendChild(iframe);
    return new Promise((resolve, reject) => {
      const channel = new MessageChannel();
      let ready = false;
      let done = false;
      let onLoaded: () => void = () => {};
      let onFailed: (e: Error) => void = () => {};
      const loaded = new Promise<void>((res, rej) => {
        onLoaded = res;
        onFailed = rej;
      });
      loaded.catch(() => {});
      const onWindowMessage = (event: MessageEvent) => {
        if (event.origin !== frameOrigin || event.source !== iframe.contentWindow) return;
        if (event.data?.type === "ready") ready = true;
      };
      window.addEventListener("message", onWindowMessage);
      const cleanup = () => {
        if (done) return;
        done = true;
        window.removeEventListener("message", onWindowMessage);
        channel.port1.onmessage = null;
        channel.port1.close();
        iframe.remove();
      };
      channel.port1.onmessage = (e) => {
        if (e.data?.type === "msg") onLoaded();
        else if (e.data?.type === "error") onFailed(loadError("shim-error", String(e.data.message || "card error")));
      };
      iframe.onload = () => {
        iframe.onload = null;
        const win = iframe.contentWindow;
        if (!win) {
          cleanup();
          reject(new Error("The card frame did not open."));
          return;
        }
        const body = content.buffer.slice(content.byteOffset, content.byteOffset + content.byteLength);
        win.postMessage({ body, mimeType }, frameOrigin, [channel.port2]);
        resolve({
          iframe,
          origin: frameOrigin,
          sendMessage: (data, transfer) => iframe.contentWindow?.postMessage(data, frameOrigin, transfer ?? []),
          fullyLoadedPromiseWithTimeout: async (ms) => {
            let timer: ReturnType<typeof setTimeout> | undefined;
            try {
              await Promise.race([
                loaded,
                new Promise((_, rej) => {
                  timer = setTimeout(() => rej(ready ? loadError("render-timeout", "The card took too long.")
                    : loadError("shim-unavailable", "The card sandbox did not load.")), ms);
                }),
              ]);
            } finally {
              if (timer !== undefined) clearTimeout(timer);
            }
          },
          dispose: cleanup,
        });
      };
      iframe.onerror = () => {
        cleanup();
        reject(new Error("The card frame did not load."));
      };
      iframe.src = `${box.frame}?origin=${encodeURIComponent(location.origin)}`;
    });
  }
}
