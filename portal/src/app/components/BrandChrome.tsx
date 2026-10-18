// The deployment brand around the app: page metadata (favicon, touch icon,
// decorative stylesheet), the announcement banner, the terms gate and the
// footer links. Everything here is the deployment's identity, never the
// selected agent's. Approval cards, errors and permission notices never sit
// inside a brand slot, so decoration can't hide or imitate them.
import { useCallback, useEffect, useState } from "react";
import { Megaphone, X } from "lucide-react";
import { t } from "../i18n/en";
import { get, post } from "../lib/api";
import type { BrandManifest } from "../lib/types";
import { Button, Notice } from "./ui";

/** Apply a brand's head metadata. Swaps the decorative stylesheet only once
 *  the new one has loaded, so a revision change never shows a mix. */
export function applyBrandHead(manifest: BrandManifest | null | undefined, faviconUrl?: string | null) {
  const setLink = (rel: string, href: string | null | undefined) => {
    let link = document.querySelector<HTMLLinkElement>(`link[rel="${rel}"]`);
    if (!href) {
      if (link && link.dataset.hzBrand) link.remove();
      return;
    }
    if (!link) {
      link = document.createElement("link");
      link.rel = rel;
      link.dataset.hzBrand = "true";
      document.head.appendChild(link);
    }
    link.href = href;
    link.removeAttribute("type");
  };
  setLink("icon", manifest?.assets.favicon ?? faviconUrl ?? null);
  setLink("apple-touch-icon", manifest?.assets.touch_icon ?? null);
  setLink("manifest", "/manifest.webmanifest");
  const cssUrl = manifest?.decorative_css_url ?? null;
  const current = document.querySelector<HTMLLinkElement>("link[data-hz-decorative]");
  if (current?.getAttribute("href") === cssUrl) return;
  if (!cssUrl) {
    current?.remove();
    return;
  }
  const next = document.createElement("link");
  next.rel = "stylesheet";
  next.href = cssUrl;
  next.dataset.hzDecorative = "true";
  next.onload = () => current?.remove();
  next.onerror = () => next.remove();
  document.head.appendChild(next);
}

type Me = { terms_required: boolean; policy_hash: string | null; dismissed_announcements: string[] };

/** The signed-in person's terms and announcement state, for this brand. */
export function useBrandMe(manifest: BrandManifest | null | undefined, signedIn: boolean) {
  const [me, setMe] = useState<Me | null>(null);
  const reload = useCallback(() => {
    if (!signedIn) {
      setMe(null);
      return;
    }
    get<Me>("/api/branding/me", { quiet401: true })
      .then(setMe)
      .catch(() => setMe(null));
  }, [signedIn]);
  useEffect(reload, [reload, manifest?.revision]);
  return { me, reload };
}

export function AnnouncementBanner({
  manifest,
  dismissed,
  onDismissed,
}: {
  manifest: BrandManifest | null | undefined;
  dismissed: string[];
  onDismissed: () => void;
}) {
  const a = manifest?.announcement;
  const [busy, setBusy] = useState(false);
  if (!a || dismissed.includes(a.id)) return null;
  const dismiss = async () => {
    setBusy(true);
    try {
      await post("/api/branding/me/announcement-dismissals", { announcement_id: a.id });
      onDismissed();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div role="status" className="flex items-start gap-2 border-b border-line bg-info-soft px-4 py-2 text-[13.5px] text-body">
      <Megaphone size={15} aria-hidden className="mt-0.5 flex-none text-info" />
      <p className="m-0 flex-1 whitespace-pre-line">{a.text_by_locale.en}</p>
      {a.dismissible && (
        <button
          type="button"
          onClick={() => void dismiss()}
          disabled={busy}
          aria-label={t.brand.dismiss}
          className="hz-icon-btn !h-7 !w-7 flex-none"
        >
          <X size={14} aria-hidden />
        </button>
      )}
    </div>
  );
}

/** Before first protected use: the deployment's terms, Accept or Sign out.
 *  Loading, decline and a failed accept never count as acceptance. */
export function TermsGate({ manifest, policyHash, onAccepted }: {
  manifest: BrandManifest;
  policyHash: string;
  onAccepted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      await post("/api/branding/me/legal-acceptances", { version: manifest.legal.version, policy_hash: policyHash });
      onAccepted();
    } catch {
      setError(t.brand.termsFailed);
    } finally {
      setBusy(false);
    }
  };
  const signOut = async () => {
    try {
      await post("/api/auth/logout", undefined, { quiet401: true });
    } finally {
      location.assign("/auth");
    }
  };
  return (
    <main id="main" className="flex min-h-dvh items-center justify-center bg-canvas px-4">
      <div className="w-full max-w-[460px] rounded-2xl border border-line bg-raised p-6">
        <h1 className="m-0 text-[20px] font-semibold text-ink">{t.brand.termsTitle(manifest.identity.name)}</h1>
        <p className="m-0 mt-2 text-[15px] text-body">{t.brand.termsBody(manifest.legal.version || "")}</p>
        {manifest.links.terms && (
          <p className="m-0 mt-3">
            <a href={manifest.links.terms} target="_blank" rel="noopener noreferrer" className="text-accent-text">
              {t.brand.readTerms}
            </a>
          </p>
        )}
        {error && (
          <Notice tone="error" className="mt-4">
            {error}
          </Notice>
        )}
        <div className="mt-6 flex gap-2">
          <Button variant="primary" loading={busy} onClick={() => void accept()}>
            {t.brand.accept}
          </Button>
          <Button onClick={() => void signOut()}>{t.account.signOut}</Button>
        </div>
      </div>
    </main>
  );
}

/** Footer text and help, privacy and terms links (the brand.footer slot). */
export function BrandFooter({ manifest, className }: { manifest: BrandManifest | null | undefined; className?: string }) {
  if (!manifest) return null;
  const links = [
    manifest.links.help && { href: manifest.links.help, label: t.brand.help },
    manifest.links.privacy && { href: manifest.links.privacy, label: t.brand.privacy },
    manifest.links.terms && { href: manifest.links.terms, label: t.brand.terms },
  ].filter(Boolean) as { href: string; label: string }[];
  if (!manifest.copy.footer && !links.length) return null;
  return (
    <div data-brand-slot="brand.footer" className={className}>
      {manifest.copy.footer && <span>{manifest.copy.footer}</span>}
      {links.map((l) => (
        <a key={l.href} href={l.href} target="_blank" rel="noopener noreferrer" className="ml-3 text-mute hover:text-ink">
          {l.label}
        </a>
      ))}
    </div>
  );
}
