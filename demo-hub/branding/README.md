# branding/

Drop files here to brand the web app. All optional. Use these lowercase names:

| Slot | Filenames | Used as |
|---|---|---|
| **logo** | `logo.svg`, `logo.png` | The hub's logo in the web app |
| **favicon** | `favicon.svg`, `favicon.png`, `favicon.ico` | The browser tab icon |
| **styles** | `custom.css` | Optional style overrides for the web app |

## Samples shipped here

`logo.svg` and `favicon.svg` ship as Hubzoid samples. Replace them with your
own, or delete them to use the Hubzoid defaults.

## Legacy Open WebUI mode

With `HUBZOID_UI=openwebui`, `hubzoid run` copies these files into Open WebUI
on every start. There, filenames are case-insensitive, `logo.webp`,
`logo.jpg` and `logo.jpeg` also work, `splash.*` (png, svg, webp, jpg, jpeg)
sets the loading screen, and when both `logo.*` and `favicon.*` exist,
`favicon.*` wins.
