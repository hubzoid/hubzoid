// The conversation's documents, for the reply's file chips (Markdown.tsx): a
// chip for one of them gets an Open button that shows it beside the chat.
// Shared and exported views have no provider, so their chips only download.
import { createContext } from "react";
import type { DocumentRefs } from "./doc-links";

export { documentOf } from "./doc-links";

export type DocumentsApi = DocumentRefs & { open: (title: string) => void };

export const DocumentsContext = createContext<DocumentsApi | null>(null);
