// /portal/ was the standalone Admin Console. Since Hubzoid 2.0 its screens live
// in the web app's Settings page (ConsoleEmbed), so an old link or bookmark
// opens the same section there, hash and query included: #/agents/sales/runs,
// #/people?status=active, #/confirm/<id>. A person's connections open their
// own Settings section.
import { useEffect } from "react";

export function settingsAddressFor(hash: string): string {
  const path = hash.replace(/^#/, "");
  const area = path.split(/[/?]/).filter(Boolean)[0] ?? "";
  if (area === "connections") return `/settings#/me/connections${path.includes("?") ? path.slice(path.indexOf("?")) : ""}`;
  if (!area || area === "home") return "/settings";
  return `/settings#${path}`;
}

export default function Portal() {
  useEffect(() => {
    location.replace(settingsAddressFor(location.hash));
  }, []);
  return null;
}
