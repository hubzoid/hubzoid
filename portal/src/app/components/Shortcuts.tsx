// Keyboard shortcuts: one global handler for the bound actions, and a dialog
// (Mod+/) that lists them and lets the person change, turn off or reset each.
import { useEffect, useState } from "react";
import { t } from "../i18n/en";
import { get, post } from "../lib/api";
import { accountPreference, setAccountPreference } from "../../lib/presentation";
import { Button, Modal, Notice } from "./ui";

type Effective = { enabled: boolean; actions: Record<string, { label: string; chord: string | null; source: string }> };

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

export function chordOf(e: KeyboardEvent): string {
  const mods: string[] = [];
  if (isMac ? e.metaKey : e.ctrlKey) mods.push("$mod");
  if (isMac && e.ctrlKey) mods.push("Ctrl");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  let key = e.key;
  if (key === " ") key = "Space";
  if (key.length === 1) key = key.toUpperCase();
  if (["Meta", "Control", "Shift", "Alt"].includes(key)) return "";
  return [...mods, key].join("+");
}

export function display(chord: string | null) {
  if (!chord) return t.shortcuts.off;
  return chord.replace("$mod", isMac ? "⌘" : "Ctrl");
}

export function useShortcuts(handlers: Record<string, () => void>) {
  const [effective, setEffective] = useState<Effective | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener("hz:shortcuts", show);
    return () => window.removeEventListener("hz:shortcuts", show);
  }, []);
  const reload = () => get<Effective>("/api/chat/shortcuts", { quiet401: true }).then(setEffective).catch(() => setEffective(null));
  useEffect(() => {
    void reload();
  }, []);
  useEffect(() => {
    if (!effective?.enabled) return;
    const byChord = new Map<string, string>();
    for (const [action, a] of Object.entries(effective.actions)) if (a.chord) byChord.set(a.chord, action);
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      const action = byChord.get(chordOf(e));
      if (!action) return;
      const run = action === "chat.shortcuts" ? () => setOpen(true) : handlers[action];
      if (!run) return;
      e.preventDefault();
      run();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [effective, handlers]);
  return { effective, open, setOpen, reload };
}

export function ShortcutsDialog({ open, onOpenChange, effective, reload }: { open: boolean; onOpenChange: (o: boolean) => void; effective: Effective | null; reload: () => void }) {
  const [recording, setRecording] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const save = async (next: Record<string, string | null>) => {
    setError(null);
    const check = await post<{ valid: boolean; error?: { message: string } }>("/api/chat/shortcuts/validate", { shortcuts: next });
    if (!check.valid) {
      setError(check.error?.message ?? t.shortcuts.invalid);
      return;
    }
    setAccountPreference("shortcuts", next);
    setTimeout(reload, 400);
  };
  const mine = (accountPreference("shortcuts") as Record<string, string | null> | undefined) ?? {};
  useEffect(() => {
    if (!recording) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      if (e.key === "Escape") return setRecording(null);
      const chord = chordOf(e);
      if (!chord) return;
      void save({ ...mine, [recording]: chord });
      setRecording(null);
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording]);
  return (
    <Modal open={open} onOpenChange={onOpenChange} title={t.shortcuts.title}>
      {!effective?.enabled ? (
        <Notice tone="info">{t.shortcuts.disabled}</Notice>
      ) : (
        <ul className="m-0 list-none space-y-1 p-0" data-testid="shortcuts">
          {Object.entries(effective.actions).map(([action, a]) => (
            <li key={action} className="flex items-center gap-2 text-[13px]">
              <span className="flex-1 text-ink">{a.label}</span>
              <kbd className="hz-kbd">{recording === action ? t.shortcuts.press : display(a.chord)}</kbd>
              <Button size="sm" variant="ghost" onClick={() => setRecording(action)}>
                {t.shortcuts.change}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void save({ ...mine, [action]: null })}>
                {t.shortcuts.disable}
              </Button>
              {action in mine && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    const next = { ...mine };
                    delete next[action];
                    void save(next);
                  }}
                >
                  {t.shortcuts.reset}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {error && <Notice tone="error" className="mt-3">{error}</Notice>}
    </Modal>
  );
}
