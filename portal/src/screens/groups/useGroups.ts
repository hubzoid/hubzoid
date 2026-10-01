import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, groupsApi, type GroupDetail, type GroupSummary } from "../../api";
import { errorText } from "../../hooks/useData";

type Loaded<T> = { key: string; data?: T; error?: string; status?: number };

/** Load one group resource with the group client (which reads the group
 *  routes' error shape). `key` null skips; reload() fetches again. */
function useGroupLoad<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<Loaded<T>>({ key: "" });
  const [revision, setRevision] = useState(0);
  // The loader belongs to `key`; keep the latest without refetching on every render.
  const loader = useRef(load);
  useEffect(() => {
    loader.current = load;
  });
  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    loader.current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ key, data });
      })
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ key, error: errorText(e), status: e instanceof ApiError ? e.status : 0 });
      });
    return () => controller.abort();
  }, [key, revision]);
  const reload = useCallback(() => setRevision((r) => r + 1), []);
  const current = key !== null && state.key === key ? state : { key: "" };
  return { data: current.data, error: current.error, status: current.status, reload };
}

export function useGroupList(enabled: boolean) {
  return useGroupLoad<GroupSummary[]>(enabled ? "list" : null, (signal) =>
    groupsApi.list(signal).then((r) => r.groups),
  );
}

export function useGroup(id: string | undefined) {
  return useGroupLoad<GroupDetail>(id ? `group:${id}` : null, (signal) =>
    groupsApi.get(id!, signal).then((r) => r.group),
  );
}

/**
 * Group names by grant subject (`group:<id>` → name), best effort: empty for a
 * viewer who can't list groups, or where groups don't exist (legacy mode).
 */
export function useGroupNames(enabled = true) {
  const [names, setNames] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    groupsApi
      .list(controller.signal)
      .then((r) => {
        if (!controller.signal.aborted)
          setNames(Object.fromEntries(r.groups.map((g) => [g.subject, g.name])));
      })
      .catch(() => {
        /* not an organization administrator, or no groups here */
      });
    return () => controller.abort();
  }, [enabled]);
  return names;
}

/** Email addresses typed or pasted into one box: commas, semicolons, spaces
 *  and new lines separate them. Lowercased and without repeats. */
export function parseEmails(raw: string): string[] {
  const out: string[] = [];
  for (const part of raw.split(/[\s,;]+/)) {
    const email = part.trim().toLowerCase();
    if (email && !out.includes(email)) out.push(email);
  }
  return out;
}

export function emailsProblem(emails: string[]): string | null {
  const bad = emails.filter((e) => !/^[^\s@,;]+@[^\s@,;]+$/.test(e));
  if (bad.length === 0) return null;
  return bad.length === 1
    ? `${bad[0]} isn’t an email address.`
    : `These aren’t email addresses: ${bad.slice(0, 3).join(", ")}${bad.length > 3 ? "…" : ""}`;
}

export function nameProblem(raw: string): string | null {
  const name = raw.trim().replace(/\s+/g, " ");
  if (!name) return "Enter a group name.";
  if (name.length > 100) return "Use a group name of at most 100 characters.";
  if (/[,;]/.test(name)) return "Group names can’t contain commas or semicolons.";
  return null;
}
