# target-coveo

`target-coveo` is a Singer target that loads documents into a **Coveo Push source** through
the [Coveo Push API](https://docs.coveo.com/en/78/). It is built with the
[Hotglue Singer SDK](https://github.com/hotgluexyz/HotglueSingerSDK) (`TargetHotglue`,
pinned to `hotglue-singer-sdk==1.0.51`) and runs on Python 3.10.

## Configuration

| Setting | Required | Description |
|---------|----------|-------------|
| `api_key` | yes | Coveo API key with edit privileges on the Push source. Sent as `Authorization: Bearer <api_key>`. Never logged or written to state. |
| `organization_id` | yes | Coveo organization ID. |
| `source_id` | yes | Coveo Push source ID. |
| `push_api_url` | no | Regional Push API base URL. Defaults to `https://api.cloud.coveo.com/push/v1`. Other values: `https://api-ca.cloud.coveo.com/push/v1`, `https://api-eu.cloud.coveo.com/push/v1`, `https://api-au.cloud.coveo.com/push/v1`, `https://apihipaa.cloud.coveo.com/push/v1`. Must be `https://*.cloud.coveo.com`; a bare host gets `/push/v1` appended. |

```json
{
  "api_key": "<coveo push api key>",
  "organization_id": "<organization id>",
  "source_id": "<push source id>",
  "push_api_url": "https://api.cloud.coveo.com/push/v1"
}
```

Inspect the live schema with `target-coveo --about`.

## Input contract: `Documents` stream

The ETL owns all reshaping. The target validates records and sends them as-is: it does
not decode JSON strings, flatten objects, fill defaults, or change permissions.

The SDK validates every RECORD against its SCHEMA message, so the SCHEMA must declare
**every** property the records contain.

### Add or update a document (default)

| Field | Required | Rules |
|-------|----------|-------|
| `externalId` | yes | Non-empty string. Control field: not sent to Coveo, but stored in the target state. |
| `documentId` | yes | Stable absolute `https://` source URL with no whitespace or control characters. Control field: sent only as the percent-encoded `documentId` query parameter, never in the body. |
| `data` | one of | Non-empty string of HTML or text, up to 5 MB UTF-8. |
| `compressedBinaryData` | one of | Non-empty, valid base64 of the raw file bytes (for example PDF or DOCX), up to 5 MB decoded. |
| `compressionType` | with `compressedBinaryData` | Must be `"UNCOMPRESSED"`. Not allowed with `data`. |
| `fileExtension` | yes | For example `".html"`, `".pdf"`, or `".docx"` (`^\.[A-Za-z0-9]{1,16}$`). |
| `permissions` | yes | Explicit Coveo permission model, given as a JSON array (see below). |
| `parentId` | no | If present, must also be an `https://` URL. |
| any other key | no | Coveo metadata, such as `title`, `author`, `date`, `uri`, or `clickableuri`. The value must be a primitive (string, number, boolean, or null) or a flat array of primitives. A JSON string stays a string. |

Key rules: reserved keys must use the exact spelling shown above (`Data` and
`CompressedBinaryData`, for example, are rejected). Two keys that differ only in case are
rejected, because Coveo metadata keys are case insensitive. `compressedBinaryDataFileId`
is not supported.

Body sent to Coveo: the record minus `externalId` and `documentId`. Nothing else is
removed, so `compressionType` is also sent in the body.

```http
PUT {push_api_url}/organizations/{organization_id}/sources/{source_id}/documents?documentId={urlencoded documentId}[&compressionType=UNCOMPRESSED]
Content-Type: application/json
```

Every query value is percent-encoded with `quote(value, safe="")`, so `:`, `/`, `?`,
`&`, `#`, `%`, `+`, and non-ASCII characters in `documentId` stay literal. The
organization and source IDs are percent-encoded in the path. Sending the same
`documentId` again replaces the item, which makes the PUT idempotent.

### Permissions

`permissions` must be a non-empty array in one of the two
[Coveo item permission models](https://docs.coveo.com/en/78/#item-permission-models).
The two models cannot be mixed in one array:

- **Simplified model**: an array of permission sets
  `{allowAnonymous?, allowedPermissions?, deniedPermissions?}`.
- **Complete model**: an array of permission levels
  `{name?, permissionSets: [<permission set>, ...]}`.

Identity entries are `{identity: non-empty string, identityType: "User" | "Group" | "VirtualGroup" | "Unknown", securityProvider?: non-empty string}`.

The target never widens access. These records are rejected and nothing is sent:

- A missing or empty `permissions` array, or `permissions` given as a JSON string.
- A permission set that neither sets `allowAnonymous: true` explicitly nor lists at least
  one `allowedPermissions` identity. The target does not default to public.
- A non-boolean `allowAnonymous`, an unknown key, an invalid `identityType`, or an empty
  `identity`.

Valid permissions are sent exactly as received, nested levels and denied identities
included.

Example (HTML page, complete model):

```json
{
  "externalId": "page-123",
  "documentId": "https://example.atlassian.net/wiki/spaces/ENG/pages/123/Runbook",
  "title": "Runbook",
  "data": "<html><body><h1>Runbook</h1></body></html>",
  "fileExtension": ".html",
  "permissions": [
    {
      "name": "Space",
      "permissionSets": [
        {
          "allowAnonymous": false,
          "allowedPermissions": [{"identity": "eng", "identityType": "Group"}],
          "deniedPermissions": [{"identity": "contractor@example.com", "identityType": "User"}]
        }
      ]
    }
  ]
}
```

Example (PDF attachment): use `"compressedBinaryData": "<base64>"`,
`"compressionType": "UNCOMPRESSED"`, and `"fileExtension": ".pdf"` in place of `data`.

### Delete a document (explicit, single item)

```json
{"externalId": "page-123", "documentId": "https://example.atlassian.net/wiki/spaces/ENG/pages/123", "_coveoAction": "delete"}
```

A delete record must contain only these three keys, and `_coveoAction` accepts no value
other than `"delete"`. The target sends
`DELETE .../documents?documentId=<encoded>&deleteChildren=false`. It never deletes child
items, and it has no bulk delete or source rebuild.

## Target state

Each record adds a bookmark to `bookmarks.Documents`:

```json
{"success": true, "hash": "...", "id": "<documentId>", "externalId": "page-123",
 "documentId": "<documentId>", "action": "upsert", "httpStatus": 202,
 "coveoStatus": "accepted", "indexed": false}
```

- `id` is the Coveo `documentId`, which gives the Hotglue mapping `externalId` → `documentId`.
- `coveoStatus: "accepted"` and `indexed: false` mean the Push API accepted the operation
  (usually HTTP 202). Coveo indexes asynchronously, and indexing can still fail after
  that. Check the source's Log Browser or Content Browser to confirm indexing.
- A failed record has `success: false`, an `error` message, and `hg_error_class`
  (`InvalidPayloadError` for validation failures and HTTP 4xx, `InvalidCredentialsError`
  for 401 and 403).

## Error handling

| Response | Behavior |
|----------|----------|
| 2xx | Record marked accepted. |
| 429, 5xx | Retried with SDK exponential backoff (5 attempts), then the record fails. |
| 401, 403 | The record fails with `InvalidCredentialsError`; no retry. |
| Other 4xx (400, 412, 413, ...) | The record fails with `InvalidPayloadError` and up to 1000 characters of the Coveo error body; no retry. |
| Validation failure | The record fails before any HTTP call. |

Failures are per record, and the run continues.

## Limitations

- Single-item pushes only. No batch or file-container API, so no items larger than 5 MB
  and no `compressedBinaryDataFileId`.
- Only `UNCOMPRESSED` binary data is accepted. Live PDF, DOCX and text pushes using this
  spelling returned HTTP 202. Acceptance is not proof of successful downstream conversion.
- The target does not create security identities or security providers. Identities named
  in `permissions` must exist in, or be resolvable by, the source's security provider.
- The target does not manage source status (`REBUILD`/`IDLE`) or `orderingId`.
- The SDK skips a record whose hash matches a successful bookmark from previous
  incremental state, so an identical re-sync is not re-pushed.
- Keep SDK `flattening_enabled` off; flattening would destroy the nested `permissions`.

## Developer setup

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
tox -e lint    # same lint checks via tox-uv
```

The tests drive the target through Singer SCHEMA and RECORD messages and mock the HTTP
boundary with `responses`. They make no network calls.
