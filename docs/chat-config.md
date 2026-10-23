# Chat configuration

Chat defaults live in the `chat:` section of `hubzoid.yaml` in the deployment
folder (for this release, `chat/config.yaml` there is read when the section is
missing). Without either, every setting has its product default and a fresh hub
works. Settings → Workspace settings → Chat defaults shows them read-only; the
file is the only place they change.

Ratings (`feedback.enabled`, `feedback.tool`, `feedback.reasons`) are described in
[Feedback on answers](feedback.md).

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

## Changing them

Edit the file (or ask Claude Code or Codex to). Every agent reads it again when
it changes, within a few seconds; there is no apply step and no copy in the
database, and history is Git. In `hubzoid.yaml` the section holds the same keys
without `version:`:

```yaml
chat:
  presentation:
    appearance:
      default_mode: dark
```

```bash
hubzoid chat config schema --json
hubzoid chat config get . --sources --json      # the values and where each comes from
hubzoid chat config validate . --file chat/config.yaml --json
```

An edit that makes the file invalid changes nothing: the last good values stay
and Settings says what is wrong. A start with an invalid file stops, naming the
line, because it holds policy (sharing, exports, file storage) that must never
fall back to its open default. `PUT /api/chat/config` answers 409 `file_owned`.
`GET /api/chat/config`, `GET /api/chat/config/schema` and
`GET /api/chat/bootstrap` (public defaults before sign-in) are unchanged.

## Models people may pick

By default there is no model picker: every reply uses the agent's own model
(`MODEL` in its `.env`), the one its builder tuned it on. To let people choose,
list the models under `chat:` in `hubzoid.yaml`. Each entry is a label and the
model id the agent's runtime accepts (Claude: `sonnet`, `opus`, `haiku` or a
full id; Codex: its model ids):

```yaml
chat:
  models:
    allow_choice: true        # false hides the picker again
    default_ref: sonnet       # optional: picked to start with
    catalog:
      sonnet: {label: Claude Sonnet, model: sonnet}
      haiku:  {label: Claude Haiku, model: haiku}
```

The choice applies to the person's next messages in that chat; the server
refuses a model that isn't on the list. A model runs on the agent's own runtime:
a Claude agent can't answer with a Codex model, so list the models of the
runtime the agent uses.

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

Chat and Settings render from one token set. `branding/theme.json` overrides
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
