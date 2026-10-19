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
