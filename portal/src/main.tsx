import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";

// One page, two apps. The bridge serves this index.html for the Console
// (/portal/, hash routes) and for the chat app (/, /c/*, /s/*, /auth*,
// /account*). Each app is its own lazy chunk so neither pays for the other:
// the Console keeps Ant Design, the chat app keeps assistant-ui.
const isConsole = /^\/portal(\/|$)/.test(location.pathname);

const Root = isConsole
  ? lazy(() => import("./Portal.tsx"))
  : lazy(() => import("./app/App.tsx"));

if (isConsole) document.title = "Hubzoid Admin Console";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Suspense fallback={null}>
      <Root />
    </Suspense>
  </StrictMode>,
);
