# branding/

Drop files here to override Open WebUI's defaults. All optional. Filenames
are case-insensitive (`Logo.svg`, `LOGO.PNG`, and `logo.svg` all work).

| Slot | Accepted filenames |
|---|---|
| **logo** | `logo.svg`, `logo.png`, `logo.webp`, `logo.jpg`, `logo.jpeg` |
| **favicon** | `favicon.svg`, `favicon.ico`, `favicon.png` |
| **splash** | `splash.png`, `splash.svg`, `splash.webp`, `splash.jpg`, `splash.jpeg` |

If both `logo.*` and `favicon.*` exist, `favicon.*` wins (Open WebUI uses
one mark in both top-bar and tab-icon positions).

## Samples shipped here

`logo.svg` and `favicon.svg` ship as Hubzoid samples. Replace them with
your own. If you delete them entirely, Open WebUI's defaults render.

## How it works

On every `hubzoid run`, the framework copies these files into Open WebUI's
internal static directory. Idempotent: edit, restart, see the change.
