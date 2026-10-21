// Pure rules of the chat runtime (runtime.ts), kept free of imports so the
// portal contract test can load them directly.

/** The ExternalStoreAdapter fields Hubzoid binds (checked against the pin). */
export const ADAPTER_FIELDS = [
  "messageRepository",
  "isRunning",
  "isLoading",
  "isDisabled",
  "isSendDisabled",
  "onNew",
  "queue",
  "onEdit",
  "onReload",
  "onResume",
  "onCancel",
  "onDelete",
  "unstable_onBranchChange",
  "setMessages",
  "onRefetchThread",
  "adapters",
] as const;

/** Fields deliberately left out: tools run and settle on the server, and
 *  import/export use the server graph codec. */
export const UNBOUND_FIELDS = ["onAddToolResult", "onResumeToolCall", "onImport", "onExportExternalState", "onLoadExternalState"] as const;

/** The queue lane comes from the message's explicit flag, never from which
 *  runtime method was called: typing during a run queues in order. */
export function laneFor(message: { steer?: boolean | undefined }): "queue" | "steer" {
  return message.steer === true ? "steer" : "queue";
}

/** A delete goes ahead only with the preview the person confirmed. */
export function deleteGate<T>(confirmed: Map<string, T>, messageId: string): T {
  const preview = confirmed.get(messageId);
  if (!preview) throw new Error("Confirm the deletion preview first.");
  confirmed.delete(messageId);
  return preview;
}

/** The composer keeps unsent text per page in this tab (Thread.tsx), keyed
 *  `hz.draft:<person>:<path>`. A conversation started on a new-chat page
 *  records that page's draft key (`hz.draft-origin:<id>`, ChatPage.tsx). Once
 *  the server accepts a message there, that draft and the conversation's own
 *  draft (same person) are forgotten when they hold the sent text, and the
 *  record is consumed. Every other draft stays (other pages, other people in
 *  this tab), and a refused send keeps its draft. A conversation without a
 *  record is left to the composer's own clean-up. */
export function forgetSentDrafts(
  storage: Pick<Storage, "getItem" | "removeItem"> & { [key: string]: unknown },
  text: string,
  conversationId: string,
): void {
  const originKey = `hz.draft-origin:${conversationId}`;
  const origin = storage.getItem(originKey);
  if (origin === null || !origin.startsWith("hz.draft:")) return;
  storage.removeItem(originKey);
  const sent = text.trim();
  if (!sent) return;
  const person = origin.slice(0, origin.indexOf(":", "hz.draft:".length) + 1); // "hz.draft:<person>:"
  const own = `${person}/c/${encodeURIComponent(conversationId)}`;
  for (const key of Object.keys(storage)) {
    const mine = key === origin || key === own || key.startsWith(own + "?");
    if (mine && storage.getItem(key)?.trim() === sent) storage.removeItem(key);
  }
}
