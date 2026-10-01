// Syntax highlighting with Shiki, loaded only when a reply contains code. The
// JavaScript regex engine avoids shipping WebAssembly, and each language is its
// own small chunk fetched the first time it appears.
import type { HighlighterCore, LanguageRegistration } from "shiki/core";

type LangModule = { default: LanguageRegistration[] };

const LANGS: Record<string, () => Promise<LangModule>> = {
  javascript: () => import("shiki/langs/javascript.mjs"),
  typescript: () => import("shiki/langs/typescript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  python: () => import("shiki/langs/python.mjs"),
  bash: () => import("shiki/langs/bash.mjs"),
  powershell: () => import("shiki/langs/powershell.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  xml: () => import("shiki/langs/xml.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  diff: () => import("shiki/langs/diff.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  java: () => import("shiki/langs/java.mjs"),
  csharp: () => import("shiki/langs/csharp.mjs"),
  php: () => import("shiki/langs/php.mjs"),
  dockerfile: () => import("shiki/langs/dockerfile.mjs"),
};

const ALIASES: Record<string, string> = {
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  node: "javascript",
  ts: "typescript",
  py: "python",
  python3: "python",
  sh: "bash",
  shell: "bash",
  shellscript: "bash",
  zsh: "bash",
  console: "bash",
  ps: "powershell",
  ps1: "powershell",
  jsonc: "json",
  json5: "json",
  yml: "yaml",
  md: "markdown",
  rs: "rust",
  cs: "csharp",
  "c#": "csharp",
  docker: "dockerfile",
  svg: "xml",
  htm: "html",
  patch: "diff",
};

let core: Promise<HighlighterCore> | null = null;
const loaded = new Map<string, Promise<void>>();
const cache = new Map<string, string>();

export function resolveLanguage(lang: string | undefined): string | null {
  const key = (lang || "").trim().toLowerCase();
  const name = ALIASES[key] ?? key;
  return LANGS[name] ? name : null;
}

function highlighter(): Promise<HighlighterCore> {
  core ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }, light, dark] = await Promise.all([
      import("shiki/core"),
      import("shiki/engine/javascript"),
      import("shiki/themes/github-light-default.mjs"),
      import("shiki/themes/github-dark-default.mjs"),
    ]);
    return createHighlighterCore({
      themes: [light.default, dark.default],
      langs: [],
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    });
  })();
  return core;
}

/** HTML for the code, or null when the language isn't one we highlight. */
export async function highlight(code: string, lang: string | undefined): Promise<string | null> {
  const name = resolveLanguage(lang);
  if (!name) return null;
  const key = `${name}\u0000${code}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const hl = await highlighter();
  let ready = loaded.get(name);
  if (!ready) {
    ready = LANGS[name]().then((m) => hl.loadLanguage(...m.default));
    loaded.set(name, ready);
  }
  await ready;
  const html = hl.codeToHtml(code, {
    lang: name,
    themes: { light: "github-light-default", dark: "github-dark-default" },
    defaultColor: false,
  });
  if (cache.size > 200) cache.clear();
  cache.set(key, html);
  return html;
}
