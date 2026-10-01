// Answer text: GitHub-flavoured markdown (tables, task lists, strikethrough),
// Shiki-highlighted code with a copy button, and download links for files the
// agent wrote (/artifacts/<conversation>/<file>).
import { memo, useEffect, useState, type ComponentPropsWithoutRef } from "react";
import {
  MarkdownTextPrimitive,
  unstable_memoizeMarkdownComponents as memoizeComponents,
  type CodeHeaderProps,
  type SyntaxHighlighterProps,
} from "@assistant-ui/react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy, Download, FileText } from "lucide-react";
import { t } from "../i18n/en";
import { fileNameFromUrl } from "../lib/format";
import { isAppPath } from "../lib/router";
import { copyText } from "../components/ShareDialog";
import { highlight, resolveLanguage } from "./highlight";

const ARTIFACT = /^(?:\/b\/[^/]+)?\/artifacts\/[^?#]+/;

export function isArtifactHref(href: string | undefined): boolean {
  if (!href) return false;
  try {
    const url = new URL(href, location.origin);
    return url.origin === location.origin && ARTIFACT.test(url.pathname);
  } catch {
    return false;
  }
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 font-sans text-xs text-mute hover:bg-hover hover:text-ink"
      onClick={async () => {
        if (await copyText(text)) setCopied(true);
      }}
      aria-label={copied ? t.copied : label}
    >
      {copied ? <Check size={13} aria-hidden /> : <Copy size={13} aria-hidden />}
      <span aria-hidden>{copied ? t.copied : t.copy}</span>
    </button>
  );
}

function CodeHeader({ language, code }: CodeHeaderProps) {
  return (
    <div className="hz-code-head">
      <span>{language && language !== "unknown" ? language : t.chat.plainText}</span>
      <CopyButton text={code} label={t.chat.copyCode} />
    </div>
  );
}

function SyntaxHighlighter({ code, language }: SyntaxHighlighterProps) {
  const [html, setHtml] = useState<{ code: string; html: string } | null>(null);
  const highlightable = !!resolveLanguage(language);
  useEffect(() => {
    if (!highlightable) return;
    let cancelled = false;
    // Streaming code changes every token; wait for a pause before colouring.
    const timer = setTimeout(() => {
      highlight(code, language)
        .then((out) => {
          if (!cancelled && out) setHtml({ code, html: out });
        })
        .catch(() => {});
    }, 120);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code, language, highlightable]);

  if (html && html.code === code)
    return <div className="hz-code-body" dangerouslySetInnerHTML={{ __html: html.html }} />;
  return (
    <div className="hz-code-body">
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

function Link({ href, children, ...rest }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  delete (rest as { node?: unknown }).node;
  if (isArtifactHref(href)) {
    const name = fileNameFromUrl(href!);
    // The agent's download footer reads "Download <file>"; the chip already says
    // download (its icon and its accessible name), so it shows the file alone.
    const text = typeof children === "string" ? children.trim().replace(/^download\s+/i, "") : "";
    const label = text || name;
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener"
        download={name}
        className="!no-underline inline-flex max-w-full items-center gap-2 rounded-lg border border-line bg-raised px-2.5 py-1.5 align-middle text-[14px] font-medium !text-ink hover:border-accent/50 hover:bg-hover"
        aria-label={t.chat.download(label)}
      >
        <FileText size={15} aria-hidden className="flex-none text-accent-text" />
        <span className="truncate">{label}</span>
        <Download size={14} aria-hidden className="flex-none text-mute" />
      </a>
    );
  }
  let external = true;
  try {
    const url = new URL(href || "", location.origin);
    external = url.origin !== location.origin || !isAppPath(url.pathname);
  } catch {
    external = true;
  }
  return (
    <a href={href} {...rest} {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}>
      {children}
    </a>
  );
}

function Table(props: ComponentPropsWithoutRef<"table"> & { node?: unknown }) {
  const { node: _node, ...rest } = props;
  void _node;
  return (
    <div className="hz-table-wrap" tabIndex={0} role="region" aria-label={t.chat.table}>
      <table {...rest} />
    </div>
  );
}

function Image(props: ComponentPropsWithoutRef<"img"> & { node?: unknown }) {
  const { node: _node, alt, ...rest } = props;
  void _node;
  return <img alt={alt ?? ""} loading="lazy" {...rest} />;
}

const components = memoizeComponents({
  a: Link,
  table: Table,
  img: Image,
  CodeHeader,
  SyntaxHighlighter,
});

const remarkPlugins = [remarkGfm];

export const MarkdownText = memo(function MarkdownText() {
  return <MarkdownTextPrimitive className="hz-md" remarkPlugins={remarkPlugins} components={components} />;
});
