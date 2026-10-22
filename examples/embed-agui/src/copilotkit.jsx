// CopilotKit: its React chat, through the CopilotKit runtime on your app's
// server (server.mjs), which adds a fresh Hubzoid pass on every call.
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { CopilotChat, CopilotKit, useInterrupt } from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";
import { me } from "./hubzoid.js";

// Hubzoid's approvals arrive as AG-UI interrupts; CopilotKit shows them in the
// chat through useInterrupt, and resolve() resumes the run with the answer.
function Approvals() {
  useInterrupt({
    render: ({ interrupt, resolve }) => (
      <div className="approval" data-testid="approval">
        <div>{interrupt?.message || "The agent asks first."}</div>
        <button onClick={() => resolve({ approved: true })}>Approve</button>
        <button onClick={() => resolve({ approved: false })}>Reject</button>
      </div>
    ),
  });
  return null;
}

function App({ user, agent }) {
  // Thread ids start with the signed-in person (server.mjs refuses others).
  const [threadId] = useState(() => `${user}:${crypto.randomUUID()}`);
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" agent={agent} useSingleEndpoint={false} enableInspector={false}>
      <Approvals />
      <CopilotChat threadId={threadId} className="copilot" />
    </CopilotKit>
  );
}

me().then(({ user, agent }) => {
  document.getElementById("who").textContent = user ? `Signed in to the app as ${user}` : "Not signed in";
  createRoot(document.getElementById("root")).render(<StrictMode><App user={user} agent={agent} /></StrictMode>);
});
