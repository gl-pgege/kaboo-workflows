# Images and file attachments

Users attach spreadsheets, PDFs and screenshots; tools produce images worth
looking at. [Chapter 19](../configuration/Chapter_19.md) documents every knob.
This recipe is the setup Kaboo runs, and why each setting is there.

## The setup

```yaml
attachments:
  default: reference
  tool: true
  base_url: ${API_BASE_URL:-http://localhost:3000}
  authorization: forwarded_props:runToken
  content_url_template: /api/storage/download/{id}

agents:
  orchestrator: {}                       # manifest + fetch tool
  chart_reviewer:
    attachments: {inline: true}          # also sees images directly
  formatter:
    attachments: none                    # never needs the files
```

- **`default: reference`** gives every agent a manifest of what the user
  attached and nothing heavier. A manifest line costs a few tokens; an inlined
  PDF costs thousands on every turn it stays in history.
- **`tool: true`** grants `list_references` and `fetch_attachment`, so an agent
  pulls a file's content only when its task needs it.
- **`base_url` + `authorization`** let the server fetch files from your own API
  with the caller's token, taken from the run's `forwardedProps`. The token is
  sent only to URLs under `base_url`; presigned and third-party URLs are
  fetched without it.
- **`content_url_template`** makes a file fetchable on *every* turn, not only
  the turn it was attached on — the client sends the file's id in state, and
  the template turns it into a URL.
- **`inline: true`** only on the agent that must see pixels, on a model that
  can. Everyone else works from the manifest.

## Tools that return images

A tool behind an MCP gateway can only answer in text, so return images as
base64 inside the result's JSON under `attachments`. The library strips them
out of the text and hands them to the model as image blocks of the same tool
result
([Chapter 19](../configuration/Chapter_19.md#media-a-tool-returns)):

```json
{
  "summary": "3 problems in 2 of 11 widgets",
  "attachments": [{"name": "Rows 1-7", "mime_type": "image/jpeg", "data": "<base64>"}]
}
```

Kaboo uses this to close the loop on dashboards: after building one, the
visualizer calls a verification tool that renders the dashboard in a headless
browser and returns screenshots, and the agent fixes what it can see — a
chart with no data, a title cut off — before it reports back. Agents that can
see their output catch problems no schema check will.

Keep images small and few: at most 8 per result are unpacked, each up to
3.75 MB, and every one stays in the agent's history.

## Custom kinds of reference

Users can cite things that are not files — a table, a dashboard, a ticket.
These arrive as references with a `kind` and an `id`, appear in the same
manifest, and are resolved by your own tools (`get_dashboard(id)`), not by
`fetch_attachment`. A data source cited this way is also what
[tag-activated skills](skills.md#which-skills-an-agent-gets) match on.

## Frontend

kaboo-react mints the reference ids, sends files as multimodal message parts on
the turn they are attached and as state on later turns, and renders the
attachment chips
([Chapter 19](../configuration/Chapter_19.md#frontend-wiring-kaboo-react)).
