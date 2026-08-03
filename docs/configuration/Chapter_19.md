# Chapter 19: Attachments & Multimodal — References for Every Agent

[← Back to Table of Contents](README.md) | [← Previous: Full Reference](Chapter_18.md)

---

Your frontend can attach things to a message — an uploaded PDF, a screenshot, or a pointer to some entity like a database table or a dashboard. kaboo-workflows calls all of these **references**, and this chapter is about getting them to the agents that need them.

By default, only the **entry** agent sees an attachment (that is handled automatically by the AG-UI layer). But in a pipeline the agent that actually needs the file is often a sub-agent three hops in. The `attachments:` config makes references reach any agent you choose — as a lightweight text **manifest**, an on-demand **tool**, or fully **inline** media that a vision model can literally see.

## The two kinds of reference

Everything cited from the frontend is one of two transports:

- **Attachment** — a file/blob (`pdf`, `png/jpeg/gif/webp`, `csv/xlsx/doc/docx/html/txt/md`, video). Sent as multimodal message parts; resolvable to bytes/text/URL by the built-in `fetch_attachment` tool.
- **Object** — a pointer to a custom entity (`table`, `dashboard`, `ticket`, …). Never a blob — just `{kind, id, name, meta}`, sent in AG-UI `state.kaboo_references`, and resolved by **your own MCP tool** (there is no generic resolver in the library).

Both show up to agents the same way: as a manifest line naming the `kind`, `id`, and `name`. What the agent *does* with that id — call `fetch_attachment`, call your `query_table` MCP tool — depends on the kind.

## Three modes

Each in-scope agent runs in one of three modes:

- **`reference`** (baseline) — the agent gets a text manifest of what exists, plus (if enabled) the shared tool to fetch/resolve on demand. Cheap; never materializes bytes.
- **`inline`** (per-agent opt-in) — the agent *additionally* receives resolved media as strands `ContentBlock`s, so a vision/doc-capable model sees the file directly. Heavier — use only where a model must look at the pixels.
- **`none`** — the agent is excluded; no manifest, no tool.

The manifest says *what exists*; the tool lets the agent *act*; inline lets the model *see*.

## Config surface

Global defaults live on the root `attachments:` block; per-agent overrides on `AgentDef.attachments`:

```yaml
attachments:
  default: reference     # reference | none — baseline for in-scope agents
  tool: true             # expose list_references / fetch_attachment
  base_url: null         # origin of the host's own attachment routes (optional)
  authorization: null    # forwarded_props:<key> | env:<VAR> (own-origin only)
  content_url_template: null  # e.g. /attachments/{id}/content (optional)

agents:
  vision_analyst:
    attachments: { inline: true }   # manifest + inline media
  researcher: {}                    # inherits default: reference + tool
  writer:
    attachments: none               # excluded
```

### Shorthands

`attachments:` on an agent accepts the same shorthand style as `history:`:

| Value | Meaning |
|-------|---------|
| _(omitted / `null`)_ | inherit the global `default` |
| `none` or `false` | excluded — no references |
| `reference` | manifest only |
| `inline` or `true` | manifest **and** inline media |
| `{ enabled: true, inline: true }` | explicit mapping |

## How it flows

1. **Frontend** (kaboo-react) mints a stable id per reference. Files added from the shared **+** / **@** popover become `InputContent` parts with `kaboo_id` / `kaboo_kind` / `kaboo_name` in the part metadata; objects picked from the same popover go into `state.kaboo_references`. Both render as inline chips in the input.
2. **`kaboo_endpoint`** parses both the message content parts and `state.kaboo_references` into a request-scoped registry (`set_references`).
3. **`ReferenceHook`** (wired per in-scope agent) injects the manifest into the agent's system prompt on `BeforeInvocationEvent`, and — for `inline` agents — prepends resolved `ContentBlock`s on `BeforeModelCallEvent`.
4. The agent reads an id from the manifest and calls `fetch_attachment(id)` (files) or your MCP tool (custom kinds).

Because the registry is request-scoped and the hook is per-agent, references reach **every** in-scope agent in a pipeline — not just the entry.

## Built-in tools

When `attachments.tool: true` (the default), in-scope agents get two tools:

- **`list_references()`** — returns every reference in the current request (`kind`, `id`, `name`, `mime_type`).
- **`fetch_attachment(reference_id)`** — resolves a file attachment: decoded text for text types, or a document/image `ContentBlock` for media. URLs are fetched server-side; base64 data is decoded.

Custom object kinds are **not** covered by `fetch_attachment` — you resolve those with your own MCP tool (e.g. `query_table(id)`). The manifest hands the agent the `kind`+`id`; wiring the resolver is the documented extension point.

## Authorized fetching & attachments beyond the first turn

By default, URL sources are fetched server-side **without credentials** — fine
for presigned/public URLs, not for files behind your API's auth. Three knobs
change that:

```yaml
attachments:
  base_url: ${API_BASE_URL}                    # your API's origin
  authorization: forwarded_props:runToken      # or env:<VAR>
  content_url_template: /attachments/{id}/content
```

- **`base_url`** — relative reference URLs (`/attachments/…`) resolve against
  it.
- **`authorization`** — where the bearer token comes from:
  `forwarded_props:<key>` reads it from the run's AG-UI `forwardedProps` (a
  run-scoped credential your backend sends per invocation); `env:<VAR>` reads
  a static token. The token is attached **only** to URLs under `base_url` —
  presigned/public URLs on other origins keep the unauthenticated default, so
  your credential never reaches third-party hosts.
- **`content_url_template`** — entries in `state.kaboo_references` with
  `kind: "attachment"` are files, not custom objects. When they carry a
  `meta.url` — or this template can synthesize one from the id — they are
  upgraded to fetchable attachment transport, so `fetch_attachment` works on
  **every** turn, not only the first (where the file also rides the message as
  a multimodal part).

Hosts with several authenticated stores or non-HTTP retrieval can go further
and register a custom strategy in code — it receives the full `Reference` for
routing:

```python
from kaboo_workflows.tools import set_reference_fetcher

def my_fetcher(url: str, *, reference=None) -> bytes | None:
    ...  # route by reference.meta, sign requests, read from object storage

set_reference_fetcher(my_fetcher)  # once at startup, before serving
```

The registered fetcher applies everywhere bytes are resolved: the
`fetch_attachment` tool, `inline` media resolution, and the entry message's
multimodal parts (kaboo routes ag-ui-strands' internal fetch through the same
funnel).

## Inline media requirements

For `inline` to work, the agent's `model:` must be vision/doc-capable, and a few limits apply (inherited from the AG-UI → strands conversion):

- **Fetchable URLs.** URL sources are fetched server-side — unauthenticated by default (presigned/public URLs), or authorized for your own origin via `attachments.base_url` / `authorization` (see above). Have your frontend `onUpload` store the file and return a fetchable URL.
- **Format allowlist.** Images (`png/jpeg/gif/webp`) and documents (`pdf/csv/doc/docx/xls/xlsx/html/txt/md`) plus video. Unsupported or absent MIME types are skipped.
- **Audio is unsupported** and dropped.
- **Inline bloats history and event-log replay.** Prefer `reference` mode by default; keep base64 out of the transport by returning URLs from `onUpload`.

## Frontend wiring (kaboo-react)

Everything is a **reference provider** registered on `<KabooProvider references={…}>` and rendered by the `KabooReferenceInput` slot. Two transports, both shown as interactive inline chips in the input:

- **Files → `uploadProvider({ onUpload })`.** Choosing "Attach a file" (from the `+` button or the `@` popover) opens the picker, uploads via `onUpload`, and drops a file chip inline. Every file is stamped with `kaboo_id` / `kaboo_kind` / `kaboo_name` and rides the message as an attachment part.
- **Custom objects → your own `ReferenceProvider`.** Selected objects sync into `state.kaboo_references` and appear inline as `@name`.

```tsx
// references.tsx — file upload + a custom object provider (database tables)
import { uploadProvider, type ReferenceProvider } from "kaboo-react";

const tableProvider: ReferenceProvider = {
  id: "table",
  label: "Tables",
  icon: <Table2 size={16} />,
  search: (q) => TABLES.filter((t) => t.label.includes(q)),
  toReference: (item) => ({
    transport: "object",     // pointer only — never a blob
    kind: "table",           // matches your MCP resolver
    id: item.id,
    name: item.label,
  }),
};

export const referenceProviders = [
  uploadProvider({ accept: "image/*,.pdf", onUpload }),
  tableProvider,
];
```

```tsx
// App.tsx — one slot drives files + objects, + and @ share one popover
import { KabooProvider, KabooReferenceInput } from "kaboo-react";

<KabooProvider runtimeUrl="/api/copilotkit" agent={entry} references={referenceProviders}>
  <CopilotChat input={KabooReferenceInput} />
</KabooProvider>
```

`KabooReferenceInput` keeps CopilotKit's native input chrome (send button, disclaimer, theme) but replaces the plain textarea with a lightweight editor where each reference is an **interactive inline chip** — click a chip to swap it for another, or its `×` to remove it. The `+` button and the `@` key open the **same** searchable popover (an "Attach a file" action plus every searchable provider's items); it dismisses on outside-click, `Escape`, or when the `@` token is cleared. On submit the editor builds the multimodal message itself: files become attachment parts, and object references ride `state.kaboo_references` (kept in sync by `useReferences()` and cleared only once the run has started, so it never gets wiped from an in-flight send). The matching MCP resolver on the pipeline (`kind: "table"` → `research_resolve_table`) turns the object pointer into real data.

`onUpload` should store the file and return a **fetchable (presigned/public) URL**. That keeps the AG-UI transport and the persisted event log small, and is required for server-side URL fetching (there is no auth on that fetch). When omitted, kaboo-react base64-encodes the file — fine for small files/tests, heavy for large ones.

> **Tips & Tricks**
>
> - Start with `reference` everywhere; add `inline` only to the specific agent whose model must see the file.
> - The id the model reads in the manifest is the same id `fetch_attachment` / your MCP tool resolves against — frontend-minted, or a deterministic server fallback when absent.
> - Referenced ids are client-supplied and untrusted. Your resolver MCP tool must authorize access.
> - Objects (tables, dashboards) are always pointers — never send them as blobs.

---

[← Back to Table of Contents](README.md)
