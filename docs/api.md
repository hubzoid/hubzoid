# The Hubzoid API

What another program can call on a Hubzoid agent: a script, a CI job, your own
app's server, or a page that embeds the chat. The deployment serves a
reference of these routes at `/docs` (and the schema at `/openapi.json`),
titled **Hubzoid API** with the installed version. Every agent has the same
routes under its own address (below). The reference lists only the public API;
the web app's own routes are left out.

## Where to send requests

Each agent has its own address under the deployment's public URL:

```text
https://hub.example.com/b/<agent>          # e.g. /b/support-agent
```

The first agent in `hubzoid.yaml` also answers at the root. Ask an agent which
model id to send:

```bash
curl -s https://hub.example.com/b/support-agent/v1/models \
  -H "Authorization: Bearer $HUBZOID_KEY"
```

On a laptop with sign-in off, the agents answer on `http://127.0.0.1:3080`
(or the port `hubzoid run` printed) with no key.

## Credentials

**A personal API key** (`hzp_…`). A person creates one in Settings, Account,
Security, and chooses what it may do:

| Key | Can |
|---|---|
| Chat | Ask agents (`/v1/chat/completions`), and read and write the person's own conversations, files and shares |
| Read | Read what the person can read in the API |
| All | Chat, read, and the manage actions the person is allowed |

A key acts as that person, within their access, and may be limited to some
agents. It never signs in, changes passwords or two-step sign-in, creates other
keys, or answers an approval or a question: a person does that in the chat or
the Inbox. Blocking the person or deleting the key stops it at once.

**An embedded app's pass.** A page that embeds the chat gets a short-lived pass
from its own server. See [embedding](embedding.md).

## Ask an agent (OpenAI compatible)

```bash
curl -s https://hub.example.com/b/support-agent/v1/chat/completions \
  -H "Authorization: Bearer $HUBZOID_KEY" -H "Content-Type: application/json" \
  -d '{"model": "support-agent", "messages": [{"role": "user", "content": "Which orders are late?"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="https://hub.example.com/b/support-agent/v1", api_key=key)
reply = client.chat.completions.create(
    model="support-agent", messages=[{"role": "user", "content": "Which orders are late?"}])
print(reply.choices[0].message.content)
```

With a key the answer is the answer text only: tool activity and thinking are
left out. `stream: true` sends Server-Sent Events with one `id` and `created`
per answer on every chunk, then `[DONE]`. Some chunks carry a status `event`
(for example a tool starting) and no text; OpenAI's clients skip them.

The agent runs with the person's access, tools and knowledge, exactly as in the
chat. A step that needs an approval waits for the person, and the answer says
so.

## Conversations, files and search

The chat app's own conversations are under `/api/conversations` (list, read,
send, branch, share) and `/api/chat/files`, `/api/chat/artifacts` and
`/api/chat/search`. They are the person's own chats, the same ones they see in
the web app. The reference at `/docs` gives each route's body and answers.

## Attachments

Every caller attaches files the same way: upload each one, then name it in the
message.

1. `POST /api/chat/files` with the file as the multipart field `file` returns
   the stored file (`file_id`, `name`, `mime`, `size`). One request per file,
   up to `HUBZOID_MAX_UPLOAD_BYTES` (25 MiB by default). No conversation is
   needed, so the first message can carry files.
2. Send the message with a file part per file. On `/v1/chat/completions`
   that is OpenAI's file part, `{"type": "file", "file": {"file_id": "..."}}`.
   In the chat API it is `{"type": "file", "file_id": "..."}`.

```bash
id=$(curl -s -H "Authorization: Bearer $KEY" -F file=@orders.csv \
  https://hub.example.com/api/chat/files | jq -r .file_id)
curl -s -H "Authorization: Bearer $KEY" https://hub.example.com/v1/chat/completions \
  -d '{"messages":[{"role":"user","content":[{"type":"text","text":"Total the orders"},
       {"type":"file","file":{"file_id":"'"$id"'"}}]}]}'
```

Images are shown to the model directly (`HUBZOID_VISION`, on by default).
Other files get a short note and the agent reads them with `read_upload`.
Base64 data URLs in a message are refused with a message saying how to upload.
A key can name only files it uploaded.

## Approvals and questions

`GET /api/requests` lists the approvals and questions waiting for the person,
across agents. Answering one needs the person, signed in: a key can read them
but never decide.

## Who is calling

`GET /api/auth/session` says who the credential acts as. `GET /api/me/access`
lists the agents and capabilities that person has.

## Errors

Errors are JSON with a `detail` (`{"code": "...", "message": "..."}` or a
sentence). `401` means no valid credential; `403` that the person or key may
not do this; `409` a conflict with a newer change; `422` a body that is not
valid; `503` that the agent is restarting.
