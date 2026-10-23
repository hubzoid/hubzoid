# branding/

Drop files here to brand the web app. All optional. Use these lowercase names:

| Slot | Filenames | Used as |
|---|---|---|
| **logo** | `logo.svg`, `logo.png` | The hub's logo in the web app |
| **favicon** | `favicon.svg`, `favicon.png`, `favicon.ico` | The browser tab icon |

## Samples shipped here

`logo.svg` and `favicon.svg` ship as Hubzoid samples. Replace them with your
own, or delete them to use the Hubzoid defaults.

## Publishing a brand

For a name, colours, copy and a reviewed rollout, describe the brand in
`branding.yaml` and publish it with `hubzoid branding` or Settings →
Workspace settings → Brand. `hubzoid branding import-legacy . --write` turns
the files here into a first `branding.yaml`. See docs/branding.md.
