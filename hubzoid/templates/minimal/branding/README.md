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

## Open WebUI mode

With `HUBZOID_UI=openwebui`, Open WebUI's own branding stays by default, as
its license requires above 50 end users. If your deployment has 50 or fewer
end users in any rolling 30 days, or you hold Open WebUI's written permission
or an enterprise license, set `HUBZOID_OWUI_REMOVE_BRANDING=50-users-or-fewer`
or `=licensed` in `.env` (see `docs/branding.md`). `hubzoid run` then copies
these files into Open WebUI on every start. There, filenames are
case-insensitive, `logo.webp`, `logo.jpg` and `logo.jpeg` also work, `splash.*` (png, svg, webp, jpg, jpeg)
sets the loading screen, and when both `logo.*` and `favicon.*` exist,
`favicon.*` wins.
