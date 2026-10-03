import { request, query, type Access, type AccessRow, type Person } from "../../api";
import { draftFor, emptyRow, type Draft } from "./plan";

/**
 * One person's access in this agent, as the editor starts from it: their row
 * when they hold anything here, else their account (name and state) with no
 * access. `known` is the account when the caller already has it.
 */
export async function loadAccessRow(hubKey: string, subject: string, known?: Person): Promise<AccessRow> {
  const [rows, person] = await Promise.all([
    request<Access>("/access" + query({ hub: hubKey, q: subject, limit: 200 })).catch(() => null),
    known
      ? Promise.resolve(known)
      : request<{ people: Person[] }>("/people" + query({ q: subject, limit: 20 }))
          .then((r) => r.people.find((p) => p.subject === subject))
          .catch(() => undefined),
  ]);
  const row = rows?.rows.find((r) => r.subject === subject);
  if (row) return row;
  return {
    ...emptyRow(subject),
    display: person?.display ?? "",
    // Unknown when the account isn't visible here; the save re-checks it.
    status: person?.status ?? "",
    suspended: person?.suspended,
    account_unavailable: person?.account_unavailable,
  };
}

/** The editor for one person, starting from exactly what they hold here. */
export function editDraft(row: AccessRow, search?: string): Draft {
  return { ...draftFor(row), search };
}
