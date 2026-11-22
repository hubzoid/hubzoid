// Which of this conversation's documents a reply's file link names. Free of
// imports so tests/app-runtime.test.mts can load it.

export type DocumentRefs = { conversationId: string; titles: Set<string> };

const ARTIFACT_PATH = /^(?:\/b\/[^/]+)?\/artifacts\/([^/?#]+)\/([^/?#]+)$/;

/** The document a reply's file link names, when it is one of this conversation's. */
export function documentOf(href: string, docs: DocumentRefs | null, origin: string): string | null {
  if (!docs) return null;
  try {
    const url = new URL(href, origin);
    const m = ARTIFACT_PATH.exec(url.pathname);
    if (!m || url.origin !== origin) return null;
    const chat = decodeURIComponent(m[1]);
    const name = decodeURIComponent(m[2]);
    return chat === `web-${docs.conversationId}` && docs.titles.has(name) ? name : null;
  } catch {
    return null;
  }
}
