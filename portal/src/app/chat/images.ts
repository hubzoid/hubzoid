// Which images in an answer load by themselves. An answer can carry an image
// whose address holds data from the chat (a tool's reply can ask the model for
// one), and showing it would send that data to the image's host without a
// click. Images from this site's own file endpoints load at once: files the
// agent wrote, uploads and the hub's branding folder. Any other image waits
// until the person chooses to load it, with its host named.

const OWN_IMAGES = [
  /^(?:\/b\/[^/]+)?\/artifacts\/[^?#]+$/,
  /^(?:\/b\/[^/]+)?\/api\/conversations\/[^/]+\/files\/[^/]+$/,
  /^(?:\/b\/[^/]+)?\/branding\/[^/]+$/,
];

export type ImageSource =
  /** On this site: shown at once. */
  | { inline: true; src: string }
  /** Anywhere else: loaded only when the person asks. */
  | { inline: false; src: string; host: string };

/** How an image address in an answer is shown, or null when it can't be. */
export function imageSource(src: string | null | undefined): ImageSource | null {
  if (!src) return null;
  let url: URL;
  try {
    url = new URL(src, location.origin);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.origin === location.origin && OWN_IMAGES.some((re) => re.test(url.pathname)))
    return { inline: true, src: url.pathname + url.search };
  return { inline: false, src: url.href, host: url.host };
}
