---
name: outline
description: Use when reading from or writing to the self-hosted Outline wiki at outlook.tatapai.site — searching documents, creating/updating pages, registering a new resource as a child page under 资源笔记, listing collections, or anything the user phrases as "笔记 / 资源笔记 / wiki / 登记到笔记". Talks to Outline REST API with a token from outline/.env. Do NOT use Notion for this — the Notion MCP token in this environment is invalid.
---

# Outline (self-hosted wiki) — `outlook.tatapai.site`

Outline runs in `outline/` on the `singapore` host (postgres + redis + outline behind NPM). When the user says "笔记 / 资源笔记 / wiki / 登记到笔记", this is the system to use.

## Auth

Token lives in `outline/.env`:

```bash
TOKEN=$(grep '^OUTLINE_API_TOKEN=' outline/.env | cut -d= -f2)
```

It's a personal API key (all permissions, owner: `chivier.humber@outlook.com` / `yeqi-huang`). Rotate via UI → Settings → API. Never paste it into commit-able files.

API base: `https://outlook.tatapai.site/api`. Every endpoint is `POST` with a JSON body, even reads. Header set:

```
Authorization: Bearer $TOKEN
Content-Type: application/json
```

## Known IDs (PolyMarket Weather workspace)

| Thing | ID |
|-------|----|
| Collection `PolyMarket Weather` | `3d157cbf-0f3f-49eb-ab6d-1e6d3caf9ede` |
| Document `资源笔记` (single source of truth for hosts/services/domains/creds-pointers) | `01979f54-3bb4-400d-bb06-e6b67c3e0699` |

"独立页面 / 子页面 in 资源笔记" = a new document with `parentDocumentId = 01979f54-...` in the `PolyMarket Weather` collection.

If the IDs ever drift (e.g. doc renamed or restored), re-derive with `documents.search` / `collections.list` (recipes below).

## Core operations

### Search

```bash
curl -s -X POST https://outlook.tatapai.site/api/documents.search \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "<keyword>"}'
```

Returns ranked matches with snippets and full `text`. Use this to locate a doc by title or body content before deciding to create vs. update.

### Get document by id

```bash
curl -s -X POST https://outlook.tatapai.site/api/documents.info \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "<id-or-urlId>"}'
```

### List collections

```bash
curl -s -X POST https://outlook.tatapai.site/api/collections.list \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{}'
```

### Create a child page under 资源笔记

This is the canonical "登记到资源笔记" flow:

```bash
curl -s -X POST https://outlook.tatapai.site/api/documents.create \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "title": "<name>",
    "collectionId": "3d157cbf-0f3f-49eb-ab6d-1e6d3caf9ede",
    "parentDocumentId": "01979f54-3bb4-400d-bb06-e6b67c3e0699",
    "publish": true,
    "text": "<markdown body>"
  }'
```

`publish: true` makes it visible immediately. Drop it (or set `false`) for drafts.

For long bodies, write the JSON to a temp file and `--data @file` — inline `-d` blows up on quoting/newlines.

### Update a document

```bash
curl -s -X POST https://outlook.tatapai.site/api/documents.update \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "<id>", "text": "<new markdown>", "append": false, "publish": true}'
```

- `append: false` (default) — replace body entirely.
- `append: true` — append to end. Use this for "add a row / new section" without rewriting the whole doc.
- `title` — pass to rename.

### Move / archive / delete

```bash
# Move under a different parent
curl -s -X POST https://outlook.tatapai.site/api/documents.move \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "<id>", "collectionId": "...", "parentDocumentId": "..."}'

# Soft delete (goes to trash)
curl -s -X POST https://outlook.tatapai.site/api/documents.delete \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "<id>"}'
```

## Style guide for 资源笔记 child pages

Match the parent doc's voice and structure — it's the contract for every entry under it.

- **Tone**: terse English, present tense.
- **Lead block**: one-line `> blockquote` summary of what this resource is and why it matters to PolyMarket Weather.
- **At-a-glance table**: repo path / language-runtime / entry point / runtime+dev deps / where data lives / data source URL / owner.
- **Layout** (when relevant): a small ASCII tree of the source dirs.
- **Run section**: copy-pasteable commands inside fenced code blocks (`bash`).
- **Integration section**: how it slots into PolyMarket Weather — what upstream/downstream it connects to.
- **Status section**: explicitly list what is **not** done yet (no scheduler, not in compose stack, no remote DB sync, etc.). Future-you reads this first.

Avoid: pasted READMEs, secrets (only pointers to where secrets live), changelogs, screenshots.

## End-to-end: register a new resource

1. `documents.search` for the resource's name to confirm it's not already there.
2. Read the resource's `README.md` / `pyproject.toml` / `package.json` / `docker-compose.yml` — do **not** trust a stale memory.
3. Draft the body in the style guide above.
4. `documents.create` with `parentDocumentId = 01979f54-...`, `collectionId = 3d157cbf-...`, `publish: true`.
5. Report the returned `url` and `id` to the user.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `401 unauthorized` | wrong / rotated token | re-read `outline/.env`; if rotated, generate new one in UI → Settings → API and update `.env` |
| `400 ValidationError: collectionId required` | `parentDocumentId` set but `collectionId` missing | always send both |
| `collection_authorization_error` | token user not a member of that collection | grant access via UI, or use a different collection |
| Body renders as one long paragraph | newlines lost in shell quoting | put the JSON in a file and use `--data @file` |
| `documents.update` truncates the doc | forgot `append: true` for additive edits | set `append: true`, or fetch existing text via `documents.info` then concat then PUT |
| Search returns nothing for a doc you just made | indexing lag (~1-2s) | retry; also try `documents.list?collectionId=...` |

## Backup

Outline state is in `outline/postgres-data/` (DB) + `outline/storage-data/` (uploads) on the singapore host:

```bash
ssh singapore 'docker exec outline-postgres pg_dump -U outline outline | gzip' \
  > backups/outline-$(date +%F).sql.gz
ssh singapore 'tar czf - -C /opt/stacks/outline storage-data' \
  > backups/outline-files-$(date +%F).tgz
```
