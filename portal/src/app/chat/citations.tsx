// Citation markers in answers. The agent's search tool returns passages with
// markers like [cite:<id>]; the answer keeps them. Here each marker becomes a
// small numbered link that opens the cited passage, fetched and re-authorised
// by the server when it is opened. A marker the server doesn't know (made up,
// or for a source since removed or no longer shared with you) shows that the
// source is unavailable; it never shows anything it can't verify.
import { useContext, useState } from "react";
import { Popover } from "radix-ui";
import { ExternalLink } from "lucide-react";
import { get, hubUrl } from "../lib/api";
import { t } from "../i18n/en";
import { FilesContext } from "./parts";

const MARK = /\[cite:([0-9a-f-]{36})\]/g;

type MdNode = { type: string; value?: string; url?: string; children?: MdNode[] };

/** A remark plugin: [cite:<uuid>] in text becomes a link to #cite-<uuid>, numbered in order of first use. */
export function remarkCitations() {
  return (tree: MdNode) => {
    const numbers = new Map<string, number>();
    const walk = (node: MdNode) => {
      if (!node.children || node.type === "code" || node.type === "inlineCode") return;
      const out: MdNode[] = [];
      for (const child of node.children) {
        if (child.type !== "text" || !child.value || !child.value.includes("[cite:")) {
          walk(child);
          out.push(child);
          continue;
        }
        let last = 0;
        for (const m of child.value.matchAll(MARK)) {
          if (m.index! > last) out.push({ type: "text", value: child.value.slice(last, m.index) });
          if (!numbers.has(m[1])) numbers.set(m[1], numbers.size + 1);
          out.push({ type: "link", url: `#cite-${m[1]}`, children: [{ type: "text", value: String(numbers.get(m[1])) }] });
          last = m.index! + m[0].length;
        }
        if (last < child.value.length) out.push({ type: "text", value: child.value.slice(last) });
      }
      node.children = out;
    };
    walk(tree);
  };
}

type Cited = { title: string; kind: string; url: string | null; passage: string; changed_since: boolean;
  locator: { page: number | null } };

export function CitationLink({ id, label }: { id: string; label: string }) {
  const files = useContext(FilesContext);
  const [state, setState] = useState<Cited | "gone" | null>(null);
  const load = () => {
    if (state) return;
    get<Cited>(hubUrl(files.apiBase, `/api/chat/retrieval/citations/${encodeURIComponent(id)}`), { quiet401: true })
      .then(setState)
      .catch(() => setState("gone"));
  };
  return (
    <Popover.Root onOpenChange={(o) => o && load()}>
      <Popover.Trigger asChild>
        <button type="button" className="hz-cite" aria-label={t.citations.open(Number(label))}>{label}</button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="hz-menu hz-cite-pop" side="top" align="start" sideOffset={6}>
          {state === null && <div className="text-[12px] text-mute">{t.citations.loading}</div>}
          {state === "gone" && <div className="text-[12.5px] text-mute">{t.citations.unavailable}</div>}
          {state && state !== "gone" && (
            <div>
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1 text-[12.5px] font-semibold text-ink">{state.title}</div>
                {state.url && /^https:\/\//.test(state.url) && (
                  <a href={state.url} target="_blank" rel="noopener noreferrer" className="hz-icon-link" aria-label={t.citations.openPage}>
                    <ExternalLink size={13} aria-hidden />
                  </a>
                )}
              </div>
              {state.locator.page ? <div className="text-[11px] text-mute">{t.citations.page(state.locator.page)}</div> : null}
              <div className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap text-[12.5px] leading-relaxed text-body">{state.passage}</div>
              {state.changed_since && <div className="mt-2 text-[11px] text-mute">{t.citations.changed}</div>}
            </div>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
