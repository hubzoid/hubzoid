# Chat configuration

`chat/config.yaml` is an optional deployment file for chat settings. Without it
every setting has its product default and a fresh hub works. The file, the CLI,
the API and Console → Settings → Chat share one schema and one apply.

## The file

```yaml
version: 1
presentation:
  locale:
    default: en
    allowed: [en]
  appearance:
    default_mode: system          # light, dark, oled or system
    allowed_modes: [light, dark, oled, system]
    default_density: comfortable  # or compact
    allowed_densities: [comfortable, compact]
    default_text_scale: 1
    allowed_text_scales: [1, 1.125, 1.25, 1.5]
  preferences:                    # what people may change for themselves
    mode: true
    density: true
    text_scale: true
    timezone: true
  timezone: UTC
  theme_source: branding/theme.json
```

Unknown keys, YAML tags, anchors and duplicate keys are refused with the file,
line and nearest known key. Settings for chat features that are not in this
release (for example `voice:`) are refused with `capability_unavailable`, never
reported as on. English is the only interface language.

## Apply

```bash
hubzoid chat config schema --json
hubzoid chat config get . --sources --json      # revision, values and where each comes from
hubzoid chat config validate . --file chat/config.yaml --json
hubzoid chat config diff . --file chat/config.yaml --json
hubzoid chat config apply . --file chat/config.yaml --expected-revision absent --idempotency-key <uuid> --json
hubzoid chat config set presentation.appearance.default_mode . --value-json '"dark"'   # edits the file only
hubzoid chat config export .
```

`--expected-revision absent` creates the first revision and fails if one
exists. After that pass the revision `get` returned. Two applies against the
same revision give one winner and a `revision_conflict` (exit code 4) for the
other. Nothing is applied in part. No restart is needed.

API: `GET /api/chat/config/schema`, `GET /api/chat/config?sources=true`,
`POST /api/chat/config/validate`, `POST /api/chat/config/diff`,
`PUT /api/chat/config` with `{"version": 1, "expected_revision": N, "config": {...}}`
and an `Idempotency-Key` header. `GET /api/chat/bootstrap` gives the public
defaults before sign-in.

## Personal preferences

People choose appearance (light, dark, black OLED or match system), text size
and spacing in Account → Appearance, within the allowed values. Signed in, the
choice follows them to every device. A choice the deployment turned off shows
"Managed by your administrator". Order: deployment lock, then account, then
this device, then the deployment default.

```bash
hubzoid chat preferences get . --user person@example.com --json
hubzoid chat preferences set . --user person@example.com --mode oled --text-scale 1.25
```

API: `GET` and `PUT /api/chat/preferences` with `expected_revision`.

## Theme

Chat and Console render from one token set. `branding/theme.json` overrides
any token per mode; everything else inherits the Hubzoid theme:

```json
{"version": 1,
 "palettes": {"light": {"action": "#B5471F", "actionText": "#FFFFFF"}},
 "typography": {"fontFamily": "system", "monoFamily": "system-mono", "baseSize": 16, "lineHeight": 1.5},
 "shape": {"controlRadius": 8, "cardRadius": 14}}
```

Tokens per mode: `canvas`, `surface`, `elevated`, `sunken`, `hover`, `pressed`,
`text`, `bodyText`, `mutedText`, `border`, `borderSoft`, `input`, `action`,
`actionText`, `actionHover`, `actionActive`, `actionSoft`, `link`, `focus`,
`selection`, `selectionText`, `danger`, `dangerText`, and `Text`,
`Background`, `Border` for `success`, `warning`, `error` and `info`. Colours
are six-digit hex. Every mode is checked for contrast (4.5:1 text, 3:1 focus
and control boundaries) and a failure names the mode, pair and ratio.

```bash
hubzoid chat theme validate . --file branding/theme.json --json
hubzoid chat theme inspect . --mode dark --json
```

The theme is published with the brand (see [branding](branding.md)).
`GET /api/chat/theme` serves the active revision.
