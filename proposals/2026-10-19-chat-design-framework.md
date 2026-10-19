# Chat design framework and one Settings page

## Problem
The native chat works, but it does not yet look or feel like the chat tools
people already use. Bordered boxes, a boxed "New chat" button, large suggestion
cards and a heavy composer make it look dated. Chats from different agents sit
in one undifferentiated list. Sending while a reply streams (queue, interrupt)
has no clear controls. Administration lives in a separate Console that shows
raw configuration keys, digests and file names, and spreads settings across
many pages.

## Why now
The chat is about to become the default interface. First impressions decide
whether a team keeps using it, and administrators should not need to read
configuration keys to change a setting.

## What should happen
- **One design system.** Semantic colour, type, spacing, radius, elevation and
  motion tokens for dark (default), light and black. The built-in palettes
  change; branded palettes keep working because components use the same
  tokens.
- **Sidebar.** Quiet rows for New chat, Search (a dialog, with a keyboard
  shortcut), Sources, Spaces and Workspace. Chats are grouped by agent when more
  than one is available (with a Recent view across agents), then by date.
  Rows show pin and options on hover, and unread or replying state.
- **Conversation.** The agent picker and title in the header. A quiet empty
  state with suggestion pills and the composer in the middle. Prose answers
  without boxes, a single activity summary for tool calls, citation chips, and a
  hover action bar (copy, retry, feedback, and a menu for the rest).
- **Composer.** One rounded surface with a + menu (upload, reuse a file, saved
  prompt, web search, compare), the model picker, dictation and a send button
  that becomes Stop. Typing while a reply streams offers Queue, or Interrupt and
  send. Queued messages sit flat above the composer.
- **Notifications.** A small toast and a bell popover; no large boxes.
- **One Settings page.** Personal and team sections in one place in the chat
  shell. Default views use plain labels; technical values (keys, digests, file
  paths, revisions) move behind a details disclosure. Existing review and
  approval steps are kept.
- **assistant-ui features.** Use more of what the library provides: quoting a
  selection into the reply, an overflow action menu with Markdown export, and
  error and reasoning primitives.

## Out of scope
New capabilities, data model changes, or removing either chat interface.
Console URLs keep working and point at the matching Settings section.

## Done means
The chat, Settings, and their menus and states match the reviewed design sample
in dark, light and on a phone, existing journeys still pass, and every visible
string is in plain language.
