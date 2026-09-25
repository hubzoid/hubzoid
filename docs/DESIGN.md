# Console design baseline

The Console follows the current Studio identity: Inter body text, JetBrains Mono
commands, warm light canvas, dark ink and the original orange accent. Small text
and filled buttons use the stronger `#B5471F` accent. Semantic colors identify
success, warning, error and information with accompanying words or icons.

`portal/src/lib/theme.ts` maps these tokens to Ant Design. `portal.css` owns the
shell and small layout overrides, including Ant Design 6 alert title classes.
One main landmark, labelled theme radios, readable output and compact messages
apply throughout. The Console does not duplicate the chat app's account system.

Inter is a byte-identical local snapshot of Studio's
`assets/fonts/inter-latin-variable.woff2`; its SIL OFL notice is adjacent in
`portal/src/assets/brand/fonts/Inter-OFL.txt`. Brand PNGs retain the existing
Studio snapshots. Builds do not read sibling repositories or request web fonts.
The build emits both font license notices under `portal_dist/licenses/`, so they
also accompany the fonts in wheels and deployed installations.

## Dark theme

Studio supplies `dark` (#151516), `darkRaised` (#222224), `darkLine` (#3C3C40),
`darkText` (#FAFAF8) and `darkMuted` (#B5B5BC). Console already uses those base
colors. The Studio PNG wordmark includes transparent padding; Console now shows
the original mark at its readable size instead of shrinking that padding into
the compact header. Dark warning helper text uses #E6BF72 and is checked against
its rendered background at a minimum contrast ratio of 4.5:1.

The dark palette retains Studio's
charcoal base, uses the original #E5572A orange with dark #0B0B0C button text,
replaces pale status-tag backgrounds with deep tinted backgrounds, and keeps
secondary text at #B5B5BC. For success, use #A1D8B6 on #183328; for warning,
#E6BF72 on #352B19. This avoids the generated pastel orange and bright light-mode
tags in a dark shell. These colors are applied to semantic tokens and Ant Design preset tags.

## Admin Console navigation and access forms

The chat sidebar places Admin Console immediately above the profile, with an
icon and tooltip in its collapsed layout. Visibility follows the authenticated
administration check and updates after sign-in/sign-out and sidebar replacement.
The capability picker uses concise labels, visible restriction states and
accessible help buttons instead of persistent descriptive paragraphs.
