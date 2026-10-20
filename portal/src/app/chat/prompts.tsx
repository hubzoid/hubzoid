// Saved prompts in the composer: pick a shared or personal prompt, fill its
// variables, and its text (rendered and checked by the server) is placed in
// the message box. Nothing is sent until you send it.
import { Select } from "../components/Select";
import { useEffect, useState, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { useAui } from "@assistant-ui/react";
import { get, hubUrl, post } from "../lib/api";
import { describeError } from "../lib/errors";
import { t } from "../i18n/en";
import { toast } from "../components/toast";
import { useFeed } from "./feed-context";

type Variable = { name: string; label?: string | null; required: boolean; options?: string[] | null; default?: string | null };
type PromptItem = { id: string; scope: "hub" | "personal"; name: string; description?: string; variables: Variable[]; invalid?: string };

/** Opened from the composer's + menu; `children` is the element it anchors to. */
export function PromptPicker({ open, onOpenChange: setOpen, children }: { open: boolean; onOpenChange: (open: boolean) => void; children: ReactNode }) {
  const aui = useAui();
  const value = useFeed();
  const [items, setItems] = useState<PromptItem[] | null>(null);
  const [chosen, setChosen] = useState<PromptItem | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const base = value?.apiBase ?? "";
  useEffect(() => {
    if (!open) return;
    setChosen(null);
    get<{ items: PromptItem[] }>(hubUrl(base, "/api/chat/prompts")).then((o) => setItems(o.items.filter((p) => !p.invalid)))
      .catch(() => setItems([]));
  }, [open, base]);
  const insert = async (p: PromptItem, vals: Record<string, string>) => {
    try {
      const out = await post<{ text: string }>(hubUrl(base, `/api/chat/prompts/${encodeURIComponent(p.id)}/render`),
        { scope: p.scope, values: vals });
      aui.composer().setText(out.text);
      setOpen(false);
    } catch (e) {
      toast(describeError(e), "error");
    }
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Anchor asChild>{children}</Popover.Anchor>
      <Popover.Portal>
        <Popover.Content className="hz-menu w-80 p-2" side="top" align="start" sideOffset={8} aria-label={t.prompts.title}>
          {!chosen ? (
            <>
              <div className="px-1 pb-1 text-[12px] font-semibold text-mute">{t.prompts.title}</div>
              {items === null && <div className="px-1 py-2 text-[13px] text-mute">{t.loading}</div>}
              {items?.length === 0 && <div className="px-1 py-2 text-[13px] text-mute">{t.prompts.empty}</div>}
              <div className="max-h-72 overflow-auto">
                {items?.map((p) => (
                  <button key={`${p.scope}:${p.id}`} type="button" className="hz-menu-item w-full !items-start"
                    onClick={() => {
                      if (!p.variables.length) void insert(p, {});
                      else {
                        setChosen(p);
                        setValues(Object.fromEntries(p.variables.map((v) => [v.name, v.default ?? ""])));
                      }
                    }}>
                    <span className="min-w-0 flex-1 text-left">
                      <span className="block truncate text-[13px] text-ink">{p.name}</span>
                      {p.description && <span className="block truncate text-[11.5px] text-mute">{p.description}</span>}
                    </span>
                    <span className="text-[10.5px] text-mute">{p.scope === "hub" ? t.prompts.shared : t.prompts.mine}</span>
                  </button>
                ))}
              </div>
            </>
          ) : (
            <form className="space-y-2 p-1" onSubmit={(e) => { e.preventDefault(); void insert(chosen, values); }}>
              <div className="text-[13px] font-semibold text-ink">{chosen.name}</div>
              {chosen.variables.map((v) => (
                <label key={v.name} className="block">
                  <span className="hz-label">{v.label || v.name}{v.required ? "" : " (optional)"}</span>
                  {v.options?.length ? (
                    <Select aria-label={v.label || v.name} value={values[v.name] ?? ""} onValueChange={value => setValues({ ...values, [v.name]: value })} options={[{value: "", label: "—"}, ...v.options.map(o => ({value: o, label: o}))]} />
                  ) : (
                    <input className="hz-input" value={values[v.name] ?? ""} onChange={(e) => setValues({ ...values, [v.name]: e.target.value })} />
                  )}
                </label>
              ))}
              <div className="flex justify-end gap-2 pt-1">
                <button type="button" className="hz-btn hz-btn-sm" onClick={() => setChosen(null)}>{t.back}</button>
                <button type="submit" className="hz-btn hz-btn-primary hz-btn-sm">{t.prompts.insert}</button>
              </div>
            </form>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
