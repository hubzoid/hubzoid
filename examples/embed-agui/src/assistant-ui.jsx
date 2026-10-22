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

// Every request to Hubzoid carries a fresh pass from your app's server.
async function withPass(url, init = {}) {
  const pass = await getPass();
  return fetch(url, { ...init, credentials: "omit", headers: { ...(init.headers || {}), Authorization: `Bearer ${pass}` } });
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
          {threads.map((t) => (
            <button key={t.threadId} data-testid="thread" className={t.threadId === threadId ? "on" : ""}
              onClick={() => runtime.threads.switchToThread(t.threadId)}>{t.title}</button>
          ))}
        </aside>
        <ThreadPrimitive.Root className="thread">
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

me().then(({ user, hubzoid: base }) => {
  document.getElementById("who").textContent = user ? `Signed in to the app as ${user}` : "Not signed in";
  createRoot(document.getElementById("root")).render(<StrictMode><App base={base} /></StrictMode>);
});
