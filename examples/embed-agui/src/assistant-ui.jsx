// assistant-ui (headless): your own chat UI on Hubzoid's AG-UI endpoint.
// Only assistant-ui's and AG-UI's own packages; nothing from Hubzoid.
import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  fromThreadMessageLike,
  generateId,
} from "@assistant-ui/react";
import { HttpAgent } from "@ag-ui/client";
import {
  fromAgUiMessages,
  useAgUiInterrupts,
  useAgUiRuntime,
  useAgUiSubmitInterruptResponses,
} from "@assistant-ui/react-ag-ui";
import { getPass, hubzoid, me } from "./hubzoid.js";

// The record the person is looking at in your app: sent with each message as
// page context (data for the agent, never a source of access).
const page = { current: { doctype: "Sales Order", name: "SO-1001" } };

// Every request to Hubzoid carries a fresh pass from your app's server.
async function withPass(url, init = {}) {
  const pass = await getPass();
  let body = init.body;
  if (typeof body === "string" && init.method === "POST" && String(url).endsWith("/api/agui")) {
    const input = JSON.parse(body);
    input.forwardedProps = { ...(input.forwardedProps || {}), hubzoid: { page: page.current } };
    body = JSON.stringify(input);
  }
  const res = await fetch(url, { ...init, body, credentials: "omit", headers: { ...(init.headers || {}), Authorization: `Bearer ${pass}` } });
  if (!res.ok && res.status !== 204) {
    // Show Hubzoid's refusal (its code and message) instead of a silent failure.
    const detail = await res.clone().json().then((j) => j.detail || j).catch(() => ({}));
    window.dispatchEvent(new CustomEvent("hubzoid-error", { detail: { status: res.status, ...detail } }));
  }
  return res;
}

// Rejoin a reply that is still being written (after a reload): Hubzoid's
// /threads/{id}/run replays it as AG-UI events; assistant-ui wants the growing
// message, so the events are folded into its parts as they arrive.
async function* rejoin(base, threadId, abortSignal) {
  const res = await withPass(`${base}/api/agui/threads/${encodeURIComponent(threadId)}/run`, { signal: abortSignal });
  if (res.status !== 200 || !res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parts = [], texts = new Map(), calls = new Map();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      if (!block.startsWith("data: ")) continue;
      const e = JSON.parse(block.slice(6));
      if (e.type === "TEXT_MESSAGE_START") { const p = { type: "text", text: "" }; texts.set(e.messageId, p); parts.push(p); }
      else if (e.type === "TEXT_MESSAGE_CONTENT") texts.get(e.messageId).text += e.delta;
      else if (e.type === "TOOL_CALL_START") { const p = { type: "tool-call", toolCallId: e.toolCallId, toolName: e.toolCallName, argsText: "", args: {} }; calls.set(e.toolCallId, p); parts.push(p); }
      else if (e.type === "TOOL_CALL_ARGS") { const p = calls.get(e.toolCallId); p.argsText += e.delta; try { p.args = JSON.parse(p.argsText); } catch { /* partial */ } }
      else if (e.type === "TOOL_CALL_RESULT") { const p = calls.get(e.toolCallId); if (p) p.result = JSON.parse(e.content); }
      else if (e.type === "RUN_FINISHED" && e.outcome?.type === "interrupt") {
        // The rejoined reply stopped to ask: keep its approvals answerable.
        yield { content: parts.map((p) => ({ ...p })), ...waiting(e.outcome.interrupts) };
        return;
      }
      else continue;
      yield { content: parts.map((p) => ({ ...p })) };
    }
  }
}

// Where assistant-ui keeps a message's open AG-UI interrupts.
const waiting = (interrupts) => ({
  status: { type: "requires-action", reason: "interrupt" },
  metadata: { custom: { agui: { interrupts } } },
});

function ToolCall({ toolName, args, result }) {
  return (
    <div className="tool" data-testid="tool-call">
      <b>{toolName}</b> <code>{JSON.stringify(args)}</code>
      {result !== undefined && <div className="tool-result">→ {typeof result === "string" ? result : JSON.stringify(result)}</div>}
    </div>
  );
}

function Message() {
  return (
    <MessagePrimitive.Root className="msg">
      <MessagePrimitive.Parts components={{ tools: { Fallback: ToolCall } }} />
    </MessagePrimitive.Root>
  );
}

function Approvals() {
  const interrupts = useAgUiInterrupts();
  const submit = useAgUiSubmitInterruptResponses();
  const [answer, setAnswer] = useState("");
  if (!interrupts.length) return null;
  const answerWith = (i, payload) => submit([{ interruptId: i.id, status: "resolved", payload }]);
  return interrupts.map((i) => i.reason === "question" ? (
    <div key={i.id} className="approval" data-testid="question">
      <div>{i.message}</div>
      {(i.metadata?.hubzoid?.options || []).map((o) => (
        <button key={o.label} onClick={() => answerWith(i, { answer: o.label })}>{o.label}</button>
      ))}
      <input value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Or type an answer" />
      <button disabled={!answer.trim()} onClick={() => answerWith(i, { answer })}>Answer</button>
      <button onClick={() => submit([{ interruptId: i.id, status: "cancelled" }])}>Skip</button>
    </div>
  ) : (
    <div key={i.id} className="approval" data-testid="approval">
      <div>{i.message || "The agent asks first."}</div>
      <button onClick={() => answerWith(i, { approved: true })}>Approve</button>
      <button onClick={() => answerWith(i, { approved: false })}>Reject</button>
    </div>
  ));
}

function Refusal() {
  const [error, setError] = useState(null);
  useEffect(() => {
    const on = (e) => setError(e.detail);
    window.addEventListener("hubzoid-error", on);
    return () => window.removeEventListener("hubzoid-error", on);
  }, []);
  if (!error) return null;
  return (
    <div className="refusal" data-testid="refusal">
      Hubzoid refused ({error.status}{error.code ? `, ${error.code}` : ""}): {error.message || "see the server log"}
      <button onClick={() => setError(null)}>×</button>
    </div>
  );
}

function PagePicker() {
  const [value, setValue] = useState(page.current.name);
  const pick = (name) => { page.current = { doctype: "Sales Order", name }; setValue(name); };
  return (
    <label className="page">Viewing in Acme ERP:{" "}
      <select data-testid="page" value={value} onChange={(e) => pick(e.target.value)}>
        {["SO-1001", "SO-1002", "SO-2001"].map((n) => <option key={n} value={n}>Sales Order {n}</option>)}
      </select>
    </label>
  );
}

function App({ base }) {
  const [threads, setThreads] = useState([]);
  const [threadId, setThreadId] = useState(() => crypto.randomUUID());
  const current = useRef(threadId);
  const agent = useMemo(() => new HttpAgent({ url: `${base}/api/agui`, threadId, fetch: withPass }), [base, threadId]);

  async function refresh() {
    const data = await hubzoid(base, "/api/agui/threads");
    setThreads(data.threads);
  }
  useEffect(() => { refresh(); }, []);

  const runtime = useAgUiRuntime({
    agent,
    resumeTranscript: "appended",            // Hubzoid keeps the history itself
    adapters: {
      threadList: {
        threadId,
        threads: threads.map((t) => ({ status: "regular", id: t.threadId, title: t.title })),
        onSwitchToNewThread: async () => {
          current.current = crypto.randomUUID();
          setThreadId(current.current);
        },
        onSwitchToThread: async (id) => {
          const t = await hubzoid(base, `/api/agui/threads/${encodeURIComponent(id)}`);
          current.current = id;
          setThreadId(id);
          // Hubzoid's history (AG-UI messages) as assistant-ui thread messages.
          // A reply still being written is left out: the rejoin replays it whole.
          const kept = t.runningMessageId
            ? t.messages.filter((m) => !String(m.id).startsWith(t.runningMessageId)) : t.messages;
          const messages = fromAgUiMessages(kept).map((m) =>
            fromThreadMessageLike(m, generateId(), { type: "complete", reason: "unknown" }));
          // Approvals still open in this chat stay answerable after a reload.
          const last = messages.at(-1);
          if (!t.running && t.interrupts.length && last?.role === "assistant") {
            const w = waiting(t.interrupts);
            messages[messages.length - 1] = { ...last, status: w.status,
              metadata: { ...last.metadata, custom: { ...last.metadata.custom, ...w.metadata.custom } } };
          }
          return { messages, unstable_resume: t.running };
        },
      },
      history: {
        load: async () => ({ messages: [] }),     // threads load through the thread list above
        append: async () => {},                   // Hubzoid keeps the history
        resume: ({ abortSignal }) => rejoin(base, current.current, abortSignal),
      },
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="layout">
        <aside>
          <button data-testid="new-chat" onClick={() => runtime.threads.switchToNewThread()}>New chat</button>
          <button data-testid="reload-threads" onClick={refresh}>Refresh</button>
          <button data-testid="stop" onClick={() =>
            hubzoid(base, `/api/agui/threads/${encodeURIComponent(current.current)}/stop`, { method: "POST" })}>Stop the reply</button>
          <button data-testid="delete" onClick={async () => {
            await hubzoid(base, `/api/agui/threads/${encodeURIComponent(current.current)}`, { method: "DELETE" });
            await refresh();
            runtime.threads.switchToNewThread();
          }}>Delete this chat</button>
          <hr />
          {threads.map((t) => (
            <button key={t.threadId} data-testid="thread" className={t.threadId === threadId ? "on" : ""}
              onClick={() => runtime.threads.switchToThread(t.threadId)}>{t.title}</button>
          ))}
        </aside>
        <ThreadPrimitive.Root className="thread">
          <PagePicker />
          <Refusal />
          <ThreadPrimitive.Viewport className="viewport">
            <ThreadPrimitive.Messages components={{ UserMessage: Message, AssistantMessage: Message }} />
            <Approvals />
          </ThreadPrimitive.Viewport>
          <ComposerPrimitive.Root className="composer">
            <ComposerPrimitive.Input data-testid="composer" placeholder="Ask the finance agent…" />
            <ComposerPrimitive.Send data-testid="send">Send</ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  );
}

me().then(({ user, hubzoid: base, mode }) => {
  document.getElementById("who").textContent = user
    ? `Signed in to the app as ${user} · ${mode === "jwt" ? "mode 2: the app's own JWT" : "mode 1: a pass"}` : "Not signed in";
  createRoot(document.getElementById("root")).render(<StrictMode><App base={base} /></StrictMode>);
});
