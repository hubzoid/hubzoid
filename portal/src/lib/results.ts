/** Presentation of workflow values. Only published artifacts become actions. */
export type ResultArtifact = { url: string; title: string };
export const shortRunId = (id: string) => id.split("@")[0]!.slice(-8);
export function resultValue(raw: string): unknown {
  try { return JSON.parse(raw); } catch { return raw; }
}
export function resultSummary(raw: string): string {
  const value = resultValue(raw);
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const row = value as Record<string, unknown>;
    for (const key of ["summary", "title", "message", "result", "status"])
      if (typeof row[key] === "string") return row[key] as string;
    return `${Object.keys(row).length} result fields`;
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}
export function resultArtifacts(raw: string): ResultArtifact[] {
  const found = new Map<string, ResultArtifact>();
  function visit(value: unknown, depth: number) {
    if (!value || typeof value !== "object" || depth > 8) return;
    if (!Array.isArray(value)) {
      const row = value as Record<string, unknown>;
      if (typeof row.id === "string" && /^a[A-Za-z0-9_-]{16,40}$/.test(row.id) && typeof row.url === "string") {
        try {
          const url = new URL(row.url, "https://hubzoid.invalid");
          // Rebuild the canonical local viewer path. A returned URL never sends
          // the user to an external site or changes artifact access.
          if (["https:", "http:"].includes(url.protocol) && url.pathname === `/portal/artifacts/${row.id}`) {
            const path = url.pathname;
            found.set(path, { url: path, title: String(row.title || row.filename || "Report") });
          }
        } catch { /* Not an artifact address. */ }
      }
    }
    for (const child of Object.values(value)) visit(child, depth + 1);
  }
  visit(resultValue(raw), 0);
  return [...found.values()];
}
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
export function workflowCommand(action: "run" | "cancel", target: string): string {
  return `hubzoid schedule ${action} '<hub-folder>' ${quote(target)}`;
}
