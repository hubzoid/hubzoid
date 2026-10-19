import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";

// The bridge serves this index.html for the chat app (/, /c/*, /s/*, /auth*,
// /settings, ...) and for /portal/, the old Console address, which only sends
// people on to the same section in Settings.
const isConsole = /^\/portal(\/|$)/.test(location.pathname);

const Root = isConsole
  ? lazy(() => import("./Portal.tsx"))
  : lazy(() => import("./app/App.tsx"));


createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Suspense fallback={null}>
      <Root />
    </Suspense>
  </StrictMode>,
);
