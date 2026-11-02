# Attachments: one path

Plan, 2 November 2026. Based on SDK 2.0.6 (tag `v2.0.6`), read and tested on a fresh hub with claude-local, then reviewed once by Codex (gpt-6.1-sol, high) and once by Fable. Not built. Status: approved by the founder on 2 November 2026 for implementation.

## The problem in one paragraph

"The attachment issue" has been fixed at least six times since August, each time in a different layer: Open WebUI upload normalisation, the web folder namespace, exports and backups reading the wrong files, long duplicate names, the message box after sending, and now the tool schema. Each fix was right. The structure that keeps producing new bugs stayed. Open WebUI is gone, so most of that structure no longer has a reason to exist.

## What 2.0.6 does today

| Way in | How the bytes arrive | Where they are stored | How the agent is told |
|---|---|---|---|
| Web chat | `POST /api/conversations/{id}/files`, one request | Twice: the conversation folder, and again in the file store | `chat/history.attachment_note` |
| Sources page, `hubzoid files upload` | Chunked resumable upload (`/api/chat/files/uploads`, 4 MiB chunks) | File store, copied into a chat folder on reuse | same |
| Email intake | Mail reader | File store, owned by the channel | intake item |
| Slack, Telegram, WhatsApp | Channel downloads, then an HTTP POST back to the bridge's own `/uploads/{chat}/{name}` | Chat folder only | `inbound/uploads._marker` (already drifted: no disk path) |
| OpenAI-compatible API (`/v1/chat/completions`) | Base64 data URLs in the message, decoded by `server._persist_attachments` (the old Open WebUI path) | Chat folder only | `server._attachment_note` |
| Terminal (`hubzoid test -f`) | Encodes the file to base64, then calls the same Open WebUI decoder | Chat folder only | `server._attachment_note` |
| Embedded chat | Not supported. Text only (415 for anything else) | | |

At model time, two regexes parse the notes back out of the prompt text: `vision_inject` to find images, and `testing_runtime` for the scripted model. An image goes from file to text marker to regex to file to base64. The prose is the protocol.

**The tool schema bug.** Tools are defined with the OpenAI Agents `@function_tool`, which by default builds a strict schema: every argument is listed as required, and a default of `None` becomes a required nullable value. That schema goes unchanged to the Claude runtime (`factory_claude.py:204`), to Codex (`factory_codex.py:417`, `dynamicTools`) and to MCP clients (`mcp_server.py:330`). Those reject a call that leaves an optional argument out. `read_upload` fails this way on the first read of any non-PDF file (reproduced twice). Two tools were switched to non-strict on 1 Oct. Nine more remain, and hub authors' own tools have the same problem because the authoring docs say `from agents import function_tool`. A strict schema cannot be loosened afterwards: it no longer says which arguments were optional, so the fix has to be where tools are defined.

## Answers to the open questions

**Chunked uploads.** Not needed. They did not come from Open WebUI. They came from the HUB-74 spec (19 Oct) for resumable large uploads to S3. The per-file cap is 25 MiB by default (configurable) and one request handles that. The chat composer already uses one request. Open WebUI, Agno and eve all upload in one request.

**Base64 in the API.** Not used by any of our channels. Slack, Telegram and WhatsApp download the file and post it to the bridge. Base64 exists for Open WebUI and outside OpenAI-compatible clients. Drop it.

**S3.** Drop for now. Local disk (the hub's data folder) only. The store keeps its small adapter interface so S3 can return as its own decision.

## How others do it

| | Upload | In the message | To the model |
|---|---|---|---|
| Agno | One multipart request with the run (`files: List[UploadFile]`) | Typed `Image`, `File`, `Audio` objects | Each model adapter turns the objects into its own blocks |
| eve (Vercel) | Channel writes bytes once into the sandbox | A typed file part with a reference (`eve-sandbox:?path=…&type=…`) | Converted at model time: images up to 3 MB and PDFs up to 20 MB sent inline, larger ones as a path |
| Vercel AI SDK, assistant-ui | One upload | `file` part with URL and media type | Provider adapter converts |
| Open WebUI | One multipart request | File id in the message | Server builds the prompt |

The shared pattern: bytes stored once, the message holds a typed reference, each runtime converts the reference itself. Nobody parses their own prose back out.

## The plan

Two results. Each is complete and production ready on its own. Neither is a phase of the other.

### Result 1. Tool schemas are correct on every runtime (2.0.7)

1. **One decorator, non-strict.** Hubzoid exports `hubzoid.tool`, which is `function_tool` with a non-strict schema: only arguments without a default are required. Every built-in tool made with a decorator uses it, including scheduler tools such as `send_email`. The three `@function_tool(strict_mode=False)` patches go, because it is now the default. About twenty tools built directly as `FunctionTool(..., strict_json_schema=False)` are already correct and stay as they are. The OpenAI runtime is told the tool is non-strict too, so it never receives a relaxed schema marked strict.
2. **Hub authors.** The authoring docs and `hubzoid init` starter use `from hubzoid import tool`. A hub tool still written with `agents.function_tool` keeps loading. When it has optional arguments, `hubzoid doctor` and the loader say in one line which tool and how to change the import.
3. **One test** walks the tool list each runtime actually receives, not just the decorator's users. It covers every tool any runtime can receive (built-ins, scheduler extras, restricted wrappers, a sample hub tool) and checks the schema each runtime gets: optional arguments are not required, required ones are, and a call that leaves the optional ones out runs.
4. **Proof:** the three-file test (CSV, PDF, image) on a fresh test hub, on claude-local, codex-local and an API model, with no failed tool calls.

### Result 2. Attachments have one path (2.1)

**Two steps, used everywhere.**

- `save(bytes, name, mime, owner)` is the only function that stores a file. It checks size and type, stores the bytes once in the hub's local file store, and returns a file id with its record (name, type, size, SHA-256, owner). Email intake, Sources and artifact versions already use this store.
- `attach(file_id, chat)` is the only function that puts a file in a chat. It checks the caller may read the file, adds the chat's reference, and makes the file appear in the chat's uploads folder under a safe unique name. Because storage is local only, the folder entry is a hard link to the same bytes, not a second copy (it copies only when the two sit on different disks). A hard link, not a symlink: deleting the file from the store leaves old chats readable instead of showing a broken attachment, and backups and exports see an ordinary file.

**A message part keeps both names.** Each file part carries `file_id` (the store id) and `name` (the file's name in the chat folder). The note, the image loader and `read_upload` use `name` only. Today's web parts already have `file_id` equal to the folder name, so old messages keep working with no migration.

**Two ways in.** Anything outside the bridge process uploads over HTTP. Code inside it calls the functions directly.

| Surface | New path |
|---|---|
| Web chat | Upload each file with one request to `POST /api/chat/files` (no conversation needed, so the first message works). Send the message with the file ids. The server calls `attach()` when it saves the message. |
| API (personal key) and embedded chat | The same upload route, with the API key or the embed token, returns a file id. Send it as a file part to `/v1/chat/completions` or the embed endpoint. The server checks the caller owns the file and calls `attach()`. Base64 data URLs are refused with a message that says how to upload. |
| Slack, Telegram, WhatsApp | Each channel runs as its own process, so it uses the API path: it downloads the file, uploads it to `POST /api/chat/files` with the bridge key, then sends a file part. Only the bridge writes the file store. The bridge's separate `/uploads/{chat}/{name}` route goes. |
| Terminal | `hubzoid test -f` calls `save()` and `attach()` directly. `hubzoid files upload` uses one request. |
| Reuse in another chat | `attach()` with the existing file id. No new bytes. |
| Email intake, Sources | Unchanged: they already `save()`. |

**One way to tell the agent.** Each stored message keeps its parts (text, file, image). Every prompt the runtime builds, including resumed runs, branch replays and queued turns, comes from those parts, not from parsing earlier prompt text. One function writes the note for a non-image file ("[file] orders.csv, text/csv, 100 bytes. Read it with read_upload."). It has two outputs, present and missing, and keeps today's missing-file wording that tells the agent not to guess. Images are handed to the runtime as a list beside the prompt, and the existing image loader (resizing, caching, limits) turns them into each runtime's image blocks.

**Images stay on by default, with today's one switch.** `HUBZOID_VISION` (default on) turns image sending off for a text-only or cost-sensitive hub. `HUBZOID_VISION_MAX_EDGE` (1568 px) and `HUBZOID_VISION_MAX_IMAGES` (the latest 4) stay. With vision off, or beyond the limit, an image gets the same note as any other file. No per-message option.

**Other files are read when needed.** The agent gets the note and reads the file with `read_upload`. It reads only what the task needs, and a long chat does not resend whole files every turn. Sending PDFs inline as well, as eve does, would add a converter per runtime and token cost for what `read_upload` already covers. The regex that finds `[Image: …]` in prompt text, the two duplicate note builders and the scripted runtime's regex go.

**Reading stays as it is.** `read_upload` and `read_upload_full` keep their names, now with correct schemas from Result 1. They read the chat's uploads folder, as today. They are needed because the runtimes' own file tools are off on purpose: claude-local allows only Hubzoid and MCP tools, with no `Read`, so `restricted/.env` can't be reached, and codex-local runs read-only in a temporary folder. `read_upload` is the one sandboxed way to read an attachment.

**What is removed.**

- Chunked resumable uploads, upload staging and expiry, and the S3 adapter.
- The second copy of every web upload, and `POST /api/conversations/{id}/files`.
- `server._persist_attachments` and base64 decoding.
- The bridge's separate `/uploads/{chat}/{name}` route. Channels use the same upload route as API clients.
- Three note builders down to one, and two prompt regexes down to none.

Estimated removal: around 1,500 of the roughly 5,000 lines involved today. This is an estimate from reading, not a measurement. The places an attachment bug can live go from about twelve to four: `save`, `attach`, the note, the read tool.

**Upgrade.** File ids, hashes and references stay as they are. Bytes already in the local file store stay where they are. Existing chat folders keep their files, so old conversations read as before. A deployment with files stored through S3 keeps running. The store already records the storage per file, so only those files answer "storage unavailable" (503), and `hubzoid doctor` lists them. The old upload routes answer 410 with the new route named. Folders follow the same data-folder rules as today (`HUBZOID_DATA_DIR`, gateway and image-based hubs).

**Proof.** One end-to-end check per surface on a test hub: web chat, terminal, Slack, WhatsApp, Telegram, API key and embedded chat. Each attaches the same CSV, PDF and image and gets the three right answers with no failed tool call. Plus: an image follow-up in a later turn, a resumed run, a branch replay, a missing file, a file deleted from the store while an old chat still uses it, a restart, and backup and restore with attachments present (the backup holds the linked files), on a writable hub and an image-based hub.

## What we will not do

- Patch tools one at a time.
- Add another helper beside the existing paths. Each item above replaces something.
- Keep base64, chunks or S3 "just in case". Each can come back as its own decision if a user needs it.

## Codex review (one round)

Codex (gpt-6.1-sol, high) reviewed the first draft on 2 Nov. Accepted after checking against the code: the schema fix must be at definition, not in a registry (a strict schema cannot be loosened afterwards) and must also tell the OpenAI runtime the tool is non-strict. Saving bytes and attaching to a chat must be two steps, because intake, Sources and artifacts store files without a chat. API and embedded need an upload that does not require a conversation. The upgrade must keep ids, hashes and references. Prompts for resumed runs and replays must come from parts too, and the image loader's resizing and caching stay. `hubzoid files upload` also used chunks. Codex-local passes schemas as `dynamicTools`, not through `mcp_server.py`. `send_email` is a scheduler-injected tool. Changed instead of accepted: Codex suggested keeping `read_upload_full` as an alias. The plan now leaves both read tools unchanged, which is simpler.

## Fable review (one round)

Fable reviewed the plan on 2 Nov. Checked against the code and accepted: Slack and the inbound channels run as separate processes (`cli.py:714`, `_channel_specs`), so they upload over HTTP like API clients instead of writing the store themselves. Web parts today use the folder name as `file_id` (`chat/history.py`), so parts carry both the store id and the name. The non-strict count was wrong: three decorator patches plus about twenty direct `FunctionTool(strict_json_schema=False)` tools, which are already correct. The chat folder entry is a hard link. The single note keeps the missing-file case. S3 files get the existing per-file 503 instead of stopping the hub (`file_service.py:311`). The plan now says why `read_upload` is needed and names `HUBZOID_VISION` as the image switch. Fable confirmed as written: `save()` and `attach()` as two calls, the chat folder link, and dropping chunks, base64 and the bridge `/uploads` route.

## Sources

SDK `v2.0.6`: `hubzoid/server.py` (`_persist_attachments`, `_attachment_note`, `/uploads` route), `hubzoid/chat/files.py`, `hubzoid/chat/file_service.py`, `hubzoid/chat/file_routes.py`, `hubzoid/chat/files_cli.py`, `hubzoid/chat/history.py`, `hubzoid/chat/routes.py`, `hubzoid/intake/service.py`, `hubzoid/inbound/uploads.py`, `hubzoid/slack/files.py`, `hubzoid/vision_inject.py`, `hubzoid/tools/files.py`, `hubzoid/factory_claude.py:204`, `hubzoid/factory_codex.py:417`, `hubzoid/mcp_server.py:330`, `hubzoid/embed/agui.py:159`, `hubzoid/cli.py` (`hubzoid test`), `docs/authoring-a-hub.md:123`. Fix history: `git log -i --grep="attach\|upload"`, commit `78787e98` for the earlier schema patch. Reference repos (read, not run): Agno (`agno/os/routers/agents/router.py`, `agno/media/media.py`), eve (`src/internal/attachments/sandbox-refs.ts`).
