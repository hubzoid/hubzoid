// Private preview of a branding draft (/branding-preview/:draftId): the exact
// candidate bytes (GET /api/branding/drafts/:id/preview, no-store) rendered on
// sample surfaces in every appearance mode, under a "Preview only" banner that
// sits outside the candidate's styling. Sign-in, sending and approving are
// disabled here: it is a picture, not a working page.
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Check, Eye, ShieldCheck, X } from "lucide-react";
import { t } from "../i18n/en";
import { get } from "../lib/api";
import type { BrandManifest } from "../lib/types";
import { PageSpinner, StateMessage } from "../components/ui";

type Preview = BrandManifest & {
  draft_id: string;
  draft_revision: number;
  digest: string;
  state: string;
  validation: { valid: boolean; problems: { code: string; message: string }[] };
  theme: { css: Record<string, Record<string, string>>; revision?: string };
};

const MODES = ["light", "dark", "oled"] as const;

export default function BrandPreviewPage({ draftId }: { draftId: string }) {
  const [data, setData] = useState<Preview | null>(null);
  const [error, setError] = useState(false);
  const [mode, setMode] = useState<(typeof MODES)[number]>("light");

  useEffect(() => {
    document.title = `${t.brand.previewTitle} · Hubzoid`;
    get<Preview>(`/api/branding/drafts/${encodeURIComponent(draftId)}/preview`)
      .then(setData)
      .catch(() => setError(true));
  }, [draftId]);

  // The candidate's decorative stylesheet, only while this page is open.
  useEffect(() => {
    const url = data?.decorative_css_url;
    if (!url) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = url;
    link.dataset.hzPreview = "true";
    document.head.appendChild(link);
    return () => link.remove();
  }, [data?.decorative_css_url]);

  if (error)
    return (
      <main className="flex min-h-dvh items-center justify-center bg-bg">
        <StateMessage title={t.brand.previewTitle}>{t.brand.previewMissing}</StateMessage>
      </main>
    );
  if (!data) return <PageSpinner />;

  const vars = (data.theme.css?.[mode] ?? {}) as CSSProperties;
  const logo = mode === "light" ? data.assets.logo_light : data.assets.logo_dark || data.assets.logo_light;
  const name = data.identity.name;
  const copy = data.copy;

  return (
    <div className="min-h-dvh bg-bg">
      <div role="status" className="sticky top-0 z-10 flex flex-wrap items-center gap-3 border-b border-line bg-warning-soft px-4 py-2 text-[13px] text-ink">
        <Eye size={15} aria-hidden />
        <strong>{t.brand.previewOnly}</strong>
        <span className="text-mute">
          draft {data.draft_id.slice(0, 8)} · revision {data.draft_revision} · {data.digest.slice(0, 19)}… · {data.state}
        </span>
        <span role="radiogroup" aria-label={t.theme.label} className="ml-auto inline-flex gap-1">
          {MODES.map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={mode === m}
              onClick={() => setMode(m)}
              className={`rounded-md px-2.5 py-1 ${mode === m ? "bg-ink text-bg" : "text-ink hover:bg-hover"}`}
            >
              {t.theme[m]}
            </button>
          ))}
        </span>
      </div>
      {!data.validation.valid && (
        <ul className="m-0 list-none border-b border-line bg-danger-soft px-4 py-2 text-[13px] text-danger">
          {data.validation.problems.map((p) => (
            <li key={p.code + p.message}>{p.message}</li>
          ))}
        </ul>
      )}
      <div
        data-theme={mode === "light" ? "light" : "dark"}
        data-testid="brand-preview-surface"
        style={{ ...vars, background: "var(--hz-bg)", color: "var(--hz-ink)", fontFamily: "var(--hz-font-sans)" }}
        className="grid gap-6 p-6 lg:grid-cols-2"
      >
        <Surface title="Sign-in">
          <div
            className="rounded-xl bg-canvas bg-cover bg-center p-6"
            style={data.assets.signin_background ? { backgroundImage: `url("${data.assets.signin_background}")` } : undefined}
          >
            <Logo logo={logo} name={name} />
            <div className="mt-4 rounded-xl border border-line bg-raised p-5">
              <h3 className="m-0 text-[18px] font-semibold text-ink">{copy.signin_title || `Sign in to ${name}`}</h3>
              <p className="m-0 mt-1 text-[13px] text-mute">{copy.signin_body || "Use your work account."}</p>
              <button type="button" disabled className="hz-btn hz-btn-primary mt-4 w-full">Continue</button>
            </div>
            <Footer data={data} />
          </div>
        </Surface>
        <Surface title="Chat">
          <div className="rounded-xl border border-line bg-bg p-4">
            <Logo logo={logo} name={name} />
            {(copy.welcome_title || copy.welcome_body) && (
              <div data-brand-slot="brand.welcome" className="mt-3">
                <p className="m-0 font-medium text-ink">{copy.welcome_title}</p>
                <p className="m-0 text-[13px] text-mute">{copy.welcome_body}</p>
              </div>
            )}
            <div className="mt-4 ml-auto w-fit max-w-[80%] rounded-2xl bg-sunken px-3 py-2 text-[14px] text-body">
              Summarise this week's open orders.
            </div>
            <p className="m-0 mt-3 text-[14px] text-body">
              There are <strong>12 open orders</strong> worth <a className="text-accent-text" href="#preview">USD 48,200</a>.
            </p>
            {data.presentation.response_watermark && (
              <p className="m-0 mt-1 text-right text-[11.5px] text-mute">{data.presentation.response_watermark}</p>
            )}
            {/* Protected: decision cards never take brand styling. */}
            <div className="mt-3 rounded-xl border border-line bg-raised p-3" data-protected="approval">
              <p className="m-0 flex items-center gap-1.5 text-[13px] font-medium text-ink">
                <ShieldCheck size={15} aria-hidden /> Approval needed: send the summary to the team
              </p>
              <div className="mt-2 flex gap-2">
                <button type="button" disabled className="hz-btn hz-btn-primary"><Check size={14} aria-hidden /> Approve</button>
                <button type="button" disabled className="hz-btn"><X size={14} aria-hidden /> Reject</button>
              </div>
            </div>
            <p className="m-0 mt-3 rounded-lg bg-danger-soft px-3 py-2 text-[13px] text-danger">
              The sample reply failed: the model is not reachable. Try again.
            </p>
            {!!copy.suggestions?.length && (
              <div className="mt-3 flex flex-wrap gap-2">
                {copy.suggestions.map((s) => (
                  <span key={s} className="rounded-full border border-line bg-raised px-3 py-1 text-[12.5px] text-body">{s}</span>
                ))}
              </div>
            )}
          </div>
        </Surface>
        <Surface title="Shared conversation">
          <div className="rounded-xl border border-line bg-bg p-4">
            <div className="flex items-center justify-between">
              <Logo logo={logo} name={name} />
              <span className="text-[12.5px] text-mute">Read-only copy</span>
            </div>
            <p className="m-0 mt-3 text-[14px] text-body">A shared answer, as a teammate sees it.</p>
          </div>
        </Surface>
        <Surface title="Console">
          <div className="overflow-hidden rounded-xl border border-line bg-raised">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-canvas text-mute">
                <tr><th className="px-3 py-2 font-medium">Agent</th><th className="px-3 py-2 font-medium">Runs</th><th className="px-3 py-2 font-medium">Status</th></tr>
              </thead>
              <tbody className="text-body">
                <tr className="border-t border-line"><td className="px-3 py-2">Operations</td><td className="px-3 py-2">42</td><td className="px-3 py-2 text-success">Healthy</td></tr>
                <tr className="border-t border-line"><td className="px-3 py-2">Support</td><td className="px-3 py-2">7</td><td className="px-3 py-2 text-warning">Waiting for approval</td></tr>
              </tbody>
            </table>
          </div>
        </Surface>
      </div>
    </div>
  );
}

function Surface({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h2 className="m-0 mb-2 text-xs font-medium uppercase tracking-wide text-mute">{title}</h2>
      {children}
    </section>
  );
}

function Logo({ logo, name }: { logo?: string; name: string }) {
  return (
    <span data-brand-slot="brand.logoFrame" className="inline-flex items-center gap-2">
      {logo && <img src={logo} alt="" className="h-7 w-7 rounded-md object-contain" />}
      <span className="text-[14px] font-semibold text-ink">{name}</span>
    </span>
  );
}

function Footer({ data }: { data: BrandManifest }) {
  if (!data.copy.footer && !data.links.help) return null;
  return (
    <p data-brand-slot="brand.footer" className="m-0 mt-4 text-center text-[12px] text-mute">
      {data.copy.footer}
      {data.links.help && <span className="ml-3 text-accent-text">{t.brand.help}</span>}
    </p>
  );
}
