// Answer text: GitHub-flavoured markdown (tables, task lists, strikethrough),
// maths (KaTeX), Mermaid diagrams once their block stops changing, Shiki-
// highlighted code with copy and collapse, tables with Copy CSV, download links
// for files the agent wrote (/artifacts/<conversation>/<file>), and images: this
// site's own at once, any other only when the person asks (images.ts). Every
// renderer is bundled here; nothing is fetched from another site.
import { memo, useEffect, useRef, useState, type ComponentPropsWithoutRef } from "react";
import {
  MarkdownTextPrimitive,
  unstable_memoizeMarkdownComponents as memoizeComponents,
  type CodeHeaderProps,
  type SyntaxHighlighterProps,
} from "@assistant-ui/react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { Check, ChevronDown, Copy, Download, FileText, ImageIcon } from "lucide-react";
import { t } from "../i18n/en";
import { fileNameFromUrl } from "../lib/format";
import { isAppPath } from "../lib/router";
import { copyText } from "../components/ShareDialog";
import { highlight, resolveLanguage } from "./highlight";
import { CitationLink, remarkCitations } from "./citations";
import { imageSource } from "./images";

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

export function CopyButton({ text, label }: { text: string | (() => string); label: string }) {
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
        if (await copyText(typeof text === "function" ? text() : text)) setCopied(true);
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

const COLLAPSE_LINES = 40;
const MERMAID_MAX = 100_000;

/** A Mermaid diagram, drawn once its source has stopped changing (a block still
 * streaming is shown as source), with strict security and no network loaders. */
function Mermaid({ code }: { code: string }) {
  const [svg, setSvg] = useState<{ code: string; svg: string } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (code.length > MERMAID_MAX) return;
    let cancelled = false;
    setFailed(false);
    const timer = setTimeout(async () => {
      try {
        const { default: mermaid } = await import("mermaid");
        const dark = document.documentElement.dataset.theme !== "light";
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default",
          maxTextSize: MERMAID_MAX, maxEdges: 1000,
          // The dark theme's edge labels sit below 4.5:1 contrast; a darker label background fixes it.
          ...(dark ? { themeVariables: { edgeLabelBackground: "#262625" } } : {}) });
        const id = `hz-mmd-${Math.random().toString(36).slice(2)}`;
        const out = await mermaid.render(id, code);
        if (!cancelled) setSvg({ code, svg: out.svg });
      } catch {
        if (!cancelled) setFailed(true);
      }
    }, 700);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code]);
  if (svg && svg.code === code)
    return <div className="hz-diagram" role="img" aria-label={t.chat.diagram} dangerouslySetInnerHTML={{ __html: svg.svg }} />;
  return (
    <div className="hz-code-body">
      {failed && <div className="hz-render-note">{t.chat.diagramFailed}</div>}
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

function SyntaxHighlighter(props: SyntaxHighlighterProps) {
  const lines = props.code.split("\n").length;
  const [open, setOpen] = useState(false);
  if (props.language === "mermaid") return <Mermaid code={props.code} />;
  if (lines <= COLLAPSE_LINES || open) return <Highlighted {...props} />;
  return (
    <div className="hz-code-collapsed">
      <Highlighted {...props} />
      <button type="button" className="hz-code-more" onClick={() => setOpen(true)}>
        <ChevronDown size={13} aria-hidden /> {t.chat.moreLines(lines - COLLAPSE_LINES)}
      </button>
    </div>
  );
}

function Highlighted({ code, language }: SyntaxHighlighterProps) {
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
  if (href?.startsWith("#cite-")) return <CitationLink id={href.slice(6)} label={String(children)} />;
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

/** A spreadsheet-safe CSV of a rendered table: quoted cells, and a cell that
 * starts like a formula (= + - @) is prefixed so it stays text. */
export function tableCsv(rows: string[][]): string {
  const cell = (v: string) => {
    const safe = /^[=+\-@\t\r]/.test(v) ? `'${v}` : v;
    return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
  };
  return rows.map((r) => r.map(cell).join(",")).join("\r\n");
}

function Table(props: ComponentPropsWithoutRef<"table"> & { node?: unknown }) {
  const { node: _node, ...rest } = props;
  void _node;
  const ref = useRef<HTMLTableElement>(null);
  const csv = () =>
    tableCsv(Array.from(ref.current?.querySelectorAll("tr") ?? []).map((tr) =>
      Array.from(tr.querySelectorAll("th,td")).map((c) => (c.textContent ?? "").trim())));
  return (
    <div className="hz-table">
      <div className="hz-table-tools">
        <CopyButton text={csv} label={t.chat.copyCsv} />
      </div>
      <div className="hz-table-wrap" tabIndex={0} role="region" aria-label={t.chat.table}>
        <table ref={ref} {...rest} />
      </div>
    </div>
  );
}

/**
 * An image in an answer. This site's own files show at once; an image anywhere
 * else could carry chat data away in its address, so it waits for a click and
 * names its host first (images.ts).
 */
function Image(props: ComponentPropsWithoutRef<"img"> & { node?: unknown }) {
  const { node: _node, alt, src, ...rest } = props;
  void _node;
  const [asked, setAsked] = useState(false);
  const source = imageSource(typeof src === "string" ? src : null);
  if (!source) return alt ? <span>{alt}</span> : null;
  if (source.inline) return <img alt={alt ?? ""} loading="lazy" {...rest} src={source.src} />;
  if (asked) return <img alt={alt ?? ""} loading="lazy" referrerPolicy="no-referrer" {...rest} src={source.src} />;
  return (
    <button
      type="button"
      onClick={() => setAsked(true)}
      title={source.src}
      className="inline-flex max-w-full items-center gap-2 rounded-lg border border-dashed border-line bg-raised px-2.5 py-1.5 text-left align-middle font-sans text-[14px] text-ink hover:border-accent/50 hover:bg-hover"
    >
      <ImageIcon size={15} aria-hidden className="flex-none text-mute" />
      <span className="min-w-0">
        <span className="block truncate font-medium">{t.chat.loadImage(source.host)}</span>
        {alt && <span className="block truncate text-[13px] text-mute">{alt}</span>}
      </span>
    </button>
  );
}

const components = memoizeComponents({
  a: Link,
  table: Table,
  img: Image,
  CodeHeader,
  SyntaxHighlighter,
});

const remarkPlugins = [remarkGfm, remarkMath, remarkCitations];
// KaTeX never runs commands that fetch or link (trust off), and shows a bad
// formula as its source instead of failing the message.
const rehypePlugins: NonNullable<ComponentPropsWithoutRef<typeof MarkdownTextPrimitive>["rehypePlugins"]> = [
  [rehypeKatex, { throwOnError: false, strict: "ignore", trust: false, output: "htmlAndMathml" }],
];

export const MarkdownText = memo(function MarkdownText() {
  return (
    <MarkdownTextPrimitive
      className="hz-md"
      remarkPlugins={remarkPlugins}
      rehypePlugins={rehypePlugins}
      components={components}
    />
  );
});
