# raw_data/

Drop unstructured source material here — code repos, document dumps,
exports, anything the agent should be able to *search through* but you
do not want to summarize into `knowledge/`.

The agent has two tools scoped to this folder:

- `list_files('raw_data/*')` — see what is here.
- `grep_data(pattern, path='raw_data/<subfolder>')` — search file contents.

Then `read_file(path, offset, limit)` to read specific files.

There is no indexing step. Add a folder, deploy the hub, the agent can
reach it on next start.
