// Popular remote MCP servers to start from in Add connector (as Mastra
// Studio's integration dialog lists providers with their sign-in kind). Only
// the server's address and how people sign in: everything else is discovered
// by Test. Each was probed with Hubzoid's own discovery (the Google ones on
// 2026-10-11, the rest on 2026-10-10); the administrator still tests before
// offering it.

export type CatalogEntry = {
  id: string;
  name: string;
  url: string;
  auth: "oauth" | "none";
  /** OAuth without dynamic registration: register Hubzoid with the provider first. */
  needsClient?: boolean;
  /** Prefilled scopes. Without them Hubzoid asks for every scope the server
   * lists, which for Google includes full access. */
  scopes?: string;
  /** What the provider needs before people connect, shown with the OAuth app note. */
  setup?: string;
  about: string;
};

const G = "https://www.googleapis.com/auth/";
const googleSetup = (api: string) =>
  `Google’s Workspace MCP servers are in Developer Preview. In Google Cloud, enable the ${api}, then create a Web application OAuth client with the redirect URI below.`;

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
  { id: "gmail", name: "Gmail", url: "https://gmailmcp.googleapis.com/mcp/v1", auth: "oauth", needsClient: true,
    scopes: `${G}gmail.readonly`, setup: googleSetup("Gmail MCP API"), about: "Read mail" },
  { id: "google_calendar", name: "Google Calendar", url: "https://calendarmcp.googleapis.com/mcp/v1", auth: "oauth", needsClient: true,
    scopes: `${G}calendar.calendarlist.readonly ${G}calendar.events.readonly ${G}calendar.events.freebusy`,
    setup: googleSetup("Calendar MCP API"), about: "Read calendars and events" },
  { id: "google_drive", name: "Google Drive", url: "https://drivemcp.googleapis.com/mcp/v1", auth: "oauth", needsClient: true,
    scopes: `${G}drive.readonly`, setup: googleSetup("Drive MCP API"), about: "Read files" },
  { id: "google_docs", name: "Google Docs", url: "https://docsmcp.googleapis.com/mcp/v1", auth: "oauth", needsClient: true,
    scopes: `${G}drive.readonly ${G}documents.readonly`, setup: googleSetup("Docs MCP API"), about: "Read documents" },
  { id: "google_sheets", name: "Google Sheets", url: "https://sheetsmcp.googleapis.com/mcp/v1", auth: "oauth", needsClient: true,
    scopes: `${G}drive.readonly ${G}spreadsheets.readonly`, setup: googleSetup("Sheets MCP API"), about: "Read spreadsheets" },
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
  return e.needsClient ? "Needs an OAuth app" : "Each person signs in";
}
