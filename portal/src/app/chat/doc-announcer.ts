// Which document, if any, the Documents pane opens on when a reply ends.
// Pure, so the ordering rules are unit-tested (tests/app-runtime.test.mts).
//
// "prime" marks every listed version as already seen (the page's first list,
// a person's own edit), "event" only refreshes the list, "settle" (a reply
// ended) opens a version not seen yet. Lists can come back out of order: only
// the newest one is shown, but a settle's answer still opens what it found, so
// a reply's new document is never lost to a list asked a moment later.

export type ListMode = "prime" | "event" | "settle";

export type ListedDocument = { artifact_id: string; current_version_id: string };

export class DocumentAnnouncer {
  private seen = new Map<string, string>();
  private asked = 0;
  private shown = 0;

  /** A list request is about to be sent; pass the ticket to ``answer``. */
  ask(): number {
    return ++this.asked;
  }

  /** A list came back: whether to show it, and the document to open (or null). */
  answer(ticket: number, mode: ListMode, list: ListedDocument[]): { show: boolean; open: string | null } {
    const show = ticket >= this.shown;
    if (show) this.shown = ticket;
    if (mode === "event" || (!show && mode !== "settle")) return { show, open: null };
    const fresh = list.find((a) => this.seen.get(a.artifact_id) !== a.current_version_id);
    for (const a of list) this.seen.set(a.artifact_id, a.current_version_id);
    return { show, open: mode === "settle" && fresh ? fresh.artifact_id : null };
  }
}
