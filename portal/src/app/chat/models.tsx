// Model choice in the chat: a picker for your next messages, the model each
// answer was asked from, side-by-side comparison of one message on two or three
// models (each answer on its own branch, browsable with the branch arrows), and
// combining compared answers into one.
import { useState } from "react";
import { Popover } from "radix-ui";
import { Combine, GitCompareArrows } from "lucide-react";
import { useAuiState } from "@assistant-ui/react";
import { t } from "../i18n/en";
import { IconButton } from "../components/ui";
import { useFeed } from "./feed-context";

export type ModelsInfo = {
  catalog: { ref: string; label: string; model: string | null }[];
  default_ref: string;
  can_choose: boolean;
  can_compare: boolean;
  max_candidates: number;
};

export function ModelPicker() {
  const value = useFeed();
  const [ref, setRef] = useState("default");
  const info = value?.models;
  if (!info?.can_choose) return null;
  return (
    <select
      className="hz-model-select"
      value={ref}
      aria-label={t.models.choose}
      title={t.models.choose}
      onChange={(e) => {
        setRef(e.target.value);
        value!.feed.model = info.catalog.find((c) => c.ref === e.target.value)?.model ?? null;
      }}
    >
      {info.catalog.map((c) => (
        <option key={c.ref} value={c.ref}>{c.label}</option>
      ))}
    </select>
  );
}

/** The model an answer was asked from, when the person chose one. */
export function ModelLabel() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const msg = value?.feed.snapshot?.messages.find((m) => m.id === id) as { requested_model?: string | null } | undefined;
  const model = msg?.requested_model;
  if (!model) return null;
  const label = value?.models?.catalog.find((c) => c.model === model)?.label ?? model;
  return <span className="hz-model-label" title={t.models.answeredBy(label)}>{label}</span>;
}

export function CompareAction() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const info = value?.models;
  if (!value || !info?.can_compare || id.startsWith("pending")) return null;
  const parent = value.feed.snapshot?.messages.find((m) => m.id === id)?.parent_id;
  if (!parent) return null;
  const run = async () => {
    setBusy(true);
    try {
      await value.feed.operation({ kind: "compare", target_message_id: parent, model_refs: picked });
      setOpen(false);
      setPicked([]);
    } catch (e) {
      value.onError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <IconButton label={t.models.compare} className="!h-7 !w-7">
          <GitCompareArrows size={14} aria-hidden />
        </IconButton>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="hz-menu w-64 p-3" side="top" align="start" sideOffset={6}>
          <div className="text-[12.5px] font-semibold text-ink">{t.models.compareTitle}</div>
          <p className="m-0 mt-1 text-[11.5px] text-mute">{t.models.compareBody(info.max_candidates)}</p>
          <div className="mt-2 space-y-1">
            {info.catalog.map((c) => (
              <label key={c.ref} className="flex items-center gap-2 text-[13px] text-ink">
                <input
                  type="checkbox"
                  checked={picked.includes(c.ref)}
                  disabled={!picked.includes(c.ref) && picked.length >= info.max_candidates}
                  onChange={(e) => setPicked(e.target.checked ? [...picked, c.ref] : picked.filter((r) => r !== c.ref))}
                />
                {c.label}
              </label>
            ))}
          </div>
          <button type="button" className="hz-btn hz-btn-primary hz-btn-sm mt-3 w-full" disabled={picked.length < 2 || busy}
            onClick={() => void run()}>
            {t.models.compareRun(picked.length)}
          </button>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** Combine the finished answers to the same message into one (synthesis). */
export function CombineAction() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const [busy, setBusy] = useState(false);
  const snap = value?.feed.snapshot;
  const me = snap?.messages.find((m) => m.id === id);
  if (!value || !snap || !me?.parent_id) return null;
  const siblings = snap.messages.filter((m) => m.parent_id === me.parent_id && m.role === "assistant" && m.status === "complete");
  const branch = value.feed.viewedBranch;
  if (siblings.length < 2 || !branch || branch.head_id !== id) return null;
  return (
    <IconButton
      label={t.models.combine(siblings.length)}
      className="!h-7 !w-7"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await value.feed.operation({ kind: "synthesize", branch_id: branch.id, parent_id: branch.head_id,
            expected_revision: branch.revision, source_message_ids: siblings.slice(0, 8).map((m) => m.id) });
        } catch (e) {
          value.onError(e);
        } finally {
          setBusy(false);
        }
      }}
    >
      <Combine size={14} aria-hidden />
    </IconButton>
  );
}
