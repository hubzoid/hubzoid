// Popular remote MCP servers to start from in Add connector (as Mastra
// Studio's integration dialog lists providers with their sign-in kind). Only
// the server's address and how people sign in: everything else is discovered
// by Test. Each was probed with Hubzoid's own discovery on 2026-10-10; the
// administrator still tests before offering it.

export type CatalogEntry = {
  id: string;
  name: string;
  url: string;
  auth: "oauth" | "none";
  /** OAuth without dynamic registration: register Hubzoid with the provider first. */
  needsClient?: boolean;
  about: string;
};

export const CATALOG: CatalogEntry[] = [
  { id: "linear", name: "Linear", url: "https://mcp.linear.app/mcp", auth: "oauth", about: "Issues and projects" },
  { id: "notion", name: "Notion", url: "https://mcp.notion.com/mcp", auth: "oauth", about: "Pages and databases" },
  { id: "atlassian", name: "Atlassian", url: "https://mcp.atlassian.com/v1/mcp", auth: "oauth", about: "Jira and Confluence" },
  { id: "sentry", name: "Sentry", url: "https://mcp.sentry.dev/mcp", auth: "oauth", about: "Errors and issues" },
  { id: "stripe", name: "Stripe", url: "https://mcp.stripe.com", auth: "oauth", about: "Payments and customers" },
  { id: "intercom", name: "Intercom", url: "https://mcp.intercom.com/mcp", auth: "oauth", about: "Conversations and contacts" },
  { id: "supabase", name: "Supabase", url: "https://mcp.supabase.com/mcp", auth: "oauth", about: "Databases and projects" },
  { id: "vercel", name: "Vercel", url: "https://mcp.vercel.com", auth: "oauth", about: "Deployments and projects" },
  { id: "canva", name: "Canva", url: "https://mcp.canva.com/mcp", auth: "oauth", about: "Designs" },
  { id: "webflow", name: "Webflow", url: "https://mcp.webflow.com/mcp", auth: "oauth", about: "Sites and CMS" },
  { id: "github", name: "GitHub", url: "https://api.githubcopilot.com/mcp/", auth: "oauth", needsClient: true, about: "Repositories and issues" },
  { id: "asana", name: "Asana", url: "https://mcp.asana.com/v2/mcp", auth: "oauth", needsClient: true, about: "Tasks and projects" },
  { id: "hubspot", name: "HubSpot", url: "https://mcp.hubspot.com", auth: "oauth", needsClient: true, about: "CRM records" },
  { id: "box", name: "Box", url: "https://mcp.box.com", auth: "oauth", needsClient: true, about: "Files and folders" },
  { id: "cloudflare_docs", name: "Cloudflare Docs", url: "https://docs.mcp.cloudflare.com/mcp", auth: "none", about: "Documentation search" },
  { id: "hugging_face", name: "Hugging Face", url: "https://huggingface.co/mcp", auth: "none", about: "Models, datasets and papers" },
];

/** The sign-in label shown on a catalog card. */
export function catalogAuthLabel(e: CatalogEntry): string {
  if (e.auth === "none") return "No sign-in";
  return e.needsClient ? "Needs a client ID" : "Each person signs in";
}
