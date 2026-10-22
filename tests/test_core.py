"""End-to-end target tests through Singer ingestion and the HTTP boundary."""

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import backoff
import pytest
import responses
from hotglue_singer_sdk.exceptions import ConfigValidationError

from target_coveo.sinks import DocumentsSink, SecurityIdentitiesSink
from target_coveo.target import TargetCoveo

API_KEY = "test-secret-api-key-0123456789"
ORG_ID = "myorg"
SOURCE_ID = "myorg-source/1"
PROVIDER_ID = "Northstar Confluence Demo SIP"
CONFIG: dict[str, Any] = {
    "api_key": API_KEY,
    "organization_id": ORG_ID,
    "source_id": SOURCE_ID,
    "security_identity_provider_id": PROVIDER_ID,
}
DOCS_URL = (
    "https://api.cloud.coveo.com/push/v1/organizations/myorg/sources/myorg-source%2F1/documents"
)
IDENTITIES_URL = (
    "https://api.cloud.coveo.com/push/v1/organizations/myorg/providers/"
    "Northstar%20Confluence%20Demo%20SIP/permissions"
)

GROUP_PERMISSIONS = [
    {
        "allowAnonymous": False,
        "allowedPermissions": [{"identity": "space-ENG-readers", "identityType": "Group"}],
    }
]


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """SDK writes listener/state files under ../.secrets relative to the cwd."""
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / ".secrets").mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(DocumentsSink, "processed_hashes", [])
    monkeypatch.setattr(SecurityIdentitiesSink, "processed_hashes", [])
    monkeypatch.setattr(
        DocumentsSink, "backoff_wait_generator", lambda _self: backoff.constant(interval=0)
    )
    monkeypatch.setattr(
        SecurityIdentitiesSink,
        "backoff_wait_generator",
        lambda _self: backoff.constant(interval=0),
    )


def html_record(**overrides: Any) -> dict:
    record = {
        "externalId": "page-123",
        "documentId": "https://example.atlassian.net/wiki/spaces/ENG/pages/123/Runbook",
        "title": "Runbook",
        "author": "Ada Lovelace",
        "date": 1760000000000,
        "labels": ["ops", "runbook"],
        "data": "<html><body><h1>Runbook</h1></body></html>",
        "fileExtension": ".html",
        "permissions": copy.deepcopy(GROUP_PERMISSIONS),
    }
    record.update(overrides)
    return record


def binary_record(content: bytes, extension: str, **overrides: Any) -> dict:
    record = {
        "externalId": f"att-{extension.strip('.')}",
        "documentId": f"https://example.atlassian.net/wiki/download/attachments/123/file{extension}",
        "title": f"Attachment{extension}",
        "compressedBinaryData": base64.b64encode(content).decode("ascii"),
        "compressionType": "UNCOMPRESSED",
        "fileExtension": extension,
        "permissions": copy.deepcopy(GROUP_PERMISSIONS),
    }
    record.update(overrides)
    return record


def run_target(
    records: list[dict],
    config: dict | None = None,
    *,
    stream: str = "Documents",
) -> tuple[dict, str]:
    """Feed SCHEMA + RECORD messages into the target and return (final state, stdout)."""
    keys = sorted({key for record in records for key in record})
    schema = {"type": "object", "properties": {key: {} for key in keys}}
    lines = [json.dumps({"type": "SCHEMA", "stream": stream, "schema": schema})]
    lines += [
        json.dumps({"type": "RECORD", "stream": stream, "record": record}) for record in records
    ]
    target = TargetCoveo(config=dict(config or CONFIG))
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        target.listen(io.StringIO("\n".join(lines) + "\n"))
    output = stdout.getvalue()
    state = json.loads(output.strip().splitlines()[-1])
    return state, output


def bookmarks(state: dict) -> list[dict]:
    return state["bookmarks"]["Documents"]


def query_of(call: responses.Call) -> dict:
    return parse_qs(urlsplit(call.request.url).query)


@responses.activate
def test_html_page_upsert_sends_contract_and_maps_state():
    responses.put(DOCS_URL, status=202)
    record = html_record()

    state, output = run_target([record])

    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert request.method == "PUT"
    assert request.url == (
        f"{DOCS_URL}?documentId=https%3A%2F%2Fexample.atlassian.net%2Fwiki%2Fspaces"
        "%2FENG%2Fpages%2F123%2FRunbook"
    )
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.body)
    expected = {k: v for k, v in record.items() if k not in ("externalId", "documentId")}
    assert body == expected

    [bookmark] = bookmarks(state)
    assert bookmark["success"] is True
    assert bookmark["externalId"] == "page-123"
    assert bookmark["id"] == record["documentId"]
    assert bookmark["documentId"] == record["documentId"]
    assert bookmark["coveoStatus"] == "accepted"
    assert bookmark["indexed"] is False
    assert bookmark["httpStatus"] == 202
    assert state["summary"]["Documents"]["success"] == 1
    assert API_KEY not in output


@responses.activate
@pytest.mark.parametrize(
    ("content", "extension"),
    [
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF", ".pdf"),
        (b"PK\x03\x04\x14\x00\x06\x00word/document.xml\x00\xff", ".docx"),
    ],
)
def test_binary_attachment_is_sent_uncompressed(content: bytes, extension: str):
    responses.put(DOCS_URL, status=202)
    record = binary_record(content, extension)

    state, _ = run_target([record])

    call = responses.calls[0]
    assert query_of(call) == {
        "documentId": [record["documentId"]],
        "compressionType": ["UNCOMPRESSED"],
    }
    body = json.loads(call.request.body)
    assert base64.b64decode(body["compressedBinaryData"]) == content
    assert body["compressionType"] == "UNCOMPRESSED"
    assert body["fileExtension"] == extension
    assert "data" not in body
    assert "externalId" not in body
    assert "documentId" not in body
    assert bookmarks(state)[0]["success"] is True


@responses.activate
def test_nested_permission_levels_are_preserved_exactly():
    responses.put(DOCS_URL, status=202)
    permissions = [
        {
            "name": "Space",
            "permissionSets": [
                {
                    "allowAnonymous": False,
                    "allowedPermissions": [
                        {"identity": "eng", "identityType": "Group", "securityProvider": "Conf"}
                    ],
                    "deniedPermissions": [
                        {"identity": "contractor@example.com", "identityType": "User"}
                    ],
                }
            ],
        },
        {
            "name": "Page restrictions",
            "permissionSets": [
                {
                    "allowedPermissions": [
                        {"identity": "ada@example.com", "identityType": "User"},
                        {"identity": "sre", "identityType": "VirtualGroup"},
                    ]
                }
            ],
        },
    ]
    record = html_record(permissions=copy.deepcopy(permissions))

    state, _ = run_target([record])

    body = json.loads(responses.calls[0].request.body)
    assert body["permissions"] == permissions
    # Nothing was widened: the target never injects allowAnonymous.
    assert "allowAnonymous" not in body["permissions"][1]["permissionSets"][0]
    assert bookmarks(state)[0]["success"] is True


@responses.activate
def test_document_id_is_percent_encoded_and_upsert_is_repeatable():
    eu_url = "https://api-eu.cloud.coveo.com/push/v1/organizations/myorg/sources/myorg-source%2F1/documents"
    responses.put(eu_url, status=202)
    document_id = "https://example.com/wiki/a+b?x=1&y=%20é#frag"
    config = {**CONFIG, "push_api_url": "https://api-eu.cloud.coveo.com/push/v1/"}
    first = html_record(documentId=document_id)
    second = html_record(documentId=document_id, title="Runbook v2")

    state, _ = run_target([first, second], config=config)

    assert len(responses.calls) == 2
    expected_query = (
        "documentId=https%3A%2F%2Fexample.com%2Fwiki%2Fa%2Bb%3Fx%3D1%26y%3D%2520%C3%A9%23frag"
    )
    for call in responses.calls:
        assert call.request.method == "PUT"
        assert call.request.url == f"{eu_url}?{expected_query}"
        assert query_of(call)["documentId"] == [document_id]
    assert json.loads(responses.calls[1].request.body)["title"] == "Runbook v2"
    assert [b["id"] for b in bookmarks(state)] == [document_id, document_id]


@responses.activate
def test_json_strings_are_not_decoded_by_target():
    responses.put(DOCS_URL, status=202)
    record = html_record(labels='["ops","runbook"]')

    run_target([record])

    assert json.loads(responses.calls[0].request.body)["labels"] == '["ops","runbook"]'


@responses.activate
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"permissions": None}, "permissions is required"),
        ({"permissions": []}, "permissions is required"),
        ({"permissions": json.dumps(GROUP_PERMISSIONS)}, "not a string"),
        ({"permissions": [{"allowAnonymous": False}]}, "must explicitly set allowAnonymous"),
        (
            {"permissions": [{"deniedPermissions": [{"identity": "x", "identityType": "User"}]}]},
            "must explicitly set allowAnonymous",
        ),
        ({"permissions": [{"allowAnonymous": "true"}]}, "must be a boolean"),
        (
            {"permissions": [{"allowedPermissions": [{"identity": "x", "identityType": "Role"}]}]},
            "identityType must be one of",
        ),
        (
            {"permissions": [{"allowedPermissions": [{"identityType": "User"}]}]},
            "identity is required",
        ),
        ({"permissions": [{"allowAnonymous": True, "public": True}]}, "unsupported keys"),
        ({"permissions": [{"name": "L1", "permissionSets": []}]}, "non-empty array"),
        (
            {
                "permissions": [
                    {"name": "L1", "permissionSets": [{"allowAnonymous": True}]},
                    {"allowAnonymous": True},
                ]
            },
            "must not mix",
        ),
    ],
)
def test_invalid_permissions_are_rejected_without_http(overrides: dict, message: str):
    record = html_record(**overrides)

    state, _ = run_target([record])

    assert len(responses.calls) == 0
    [bookmark] = bookmarks(state)
    assert bookmark["success"] is False
    assert bookmark["externalId"] == "page-123"
    assert bookmark["hg_error_class"] == "InvalidPayloadError"
    assert message in bookmark["error"]


@responses.activate
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"externalId": None}, "externalId is required"),
        ({"documentId": "http://example.com/page"}, "absolute https:// URL"),
        ({"documentId": "/wiki/page"}, "absolute https:// URL"),
        ({"documentId": "https://example.com/a page"}, "whitespace"),
        ({"compressedBinaryData": "AAAA"}, "exactly one of data or compressedBinaryData"),
        ({"fileExtension": "html"}, "fileExtension"),
        ({"compressionType": "UNCOMPRESSED"}, "only allowed with compressedBinaryData"),
        ({"Title": "dup"}, "collide"),
        ({"Data": "<p/>"}, "must be spelled 'data'"),
        ({"space": {"key": "ENG"}}, "must be flattened by the ETL"),
        ({"_coveoAction": "upsert"}, "only supports the value 'delete'"),
    ],
)
def test_invalid_documents_are_rejected_without_http(overrides: dict, message: str):
    state, _ = run_target([html_record(**overrides)])

    assert len(responses.calls) == 0
    [bookmark] = bookmarks(state)
    assert bookmark["success"] is False
    assert message in bookmark["error"]


@responses.activate
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"compressionType": None}, "compressionType must be UNCOMPRESSED"),
        ({"compressionType": "GZIP"}, "compressionType must be UNCOMPRESSED"),
        ({"compressedBinaryData": "not base64!!"}, "valid base64"),
    ],
)
def test_invalid_binary_documents_are_rejected(overrides: dict, message: str):
    record = binary_record(b"%PDF-1.7", ".pdf", **overrides)
    record = {k: v for k, v in record.items() if v is not None}

    state, _ = run_target([record])

    assert len(responses.calls) == 0
    assert message in bookmarks(state)[0]["error"]


@responses.activate
def test_explicit_delete_targets_single_document():
    responses.delete(DOCS_URL, status=202)
    record = {
        "externalId": "page-123",
        "documentId": "https://example.atlassian.net/wiki/spaces/ENG/pages/123",
        "_coveoAction": "delete",
    }

    state, _ = run_target([record])

    call = responses.calls[0]
    assert call.request.method == "DELETE"
    assert call.request.body is None
    assert query_of(call) == {"documentId": [record["documentId"]], "deleteChildren": ["false"]}
    [bookmark] = bookmarks(state)
    assert bookmark["success"] is True
    assert bookmark["action"] == "delete"
    assert bookmark["id"] == record["documentId"]


@responses.activate
def test_delete_with_payload_fields_is_rejected():
    record = html_record(_coveoAction="delete")

    state, _ = run_target([record])

    assert len(responses.calls) == 0
    assert "delete records only accept" in bookmarks(state)[0]["error"]


@responses.activate
@pytest.mark.parametrize(
    ("status", "error_class"),
    [(400, "InvalidPayloadError"), (412, "InvalidPayloadError"), (401, "InvalidCredentialsError")],
)
def test_client_errors_fail_record_without_retry(status: int, error_class: str):
    responses.put(DOCS_URL, status=status, json={"errorCode": "INVALID_PARAMETER"})

    state, output = run_target([html_record()])

    assert len(responses.calls) == 1
    [bookmark] = bookmarks(state)
    assert bookmark["success"] is False
    assert bookmark["hg_error_class"] == error_class
    assert f"HTTP {status}" in bookmark["error"]
    assert "INVALID_PARAMETER" in bookmark["error"]
    assert API_KEY not in output


@responses.activate
def test_throttling_is_retried_then_accepted():
    responses.put(DOCS_URL, status=429)
    responses.put(DOCS_URL, status=202)

    state, _ = run_target([html_record()])

    assert len(responses.calls) == 2
    assert bookmarks(state)[0]["success"] is True


@responses.activate
def test_server_errors_exhaust_retries_and_fail_record():
    responses.put(DOCS_URL, status=503)

    state, output = run_target([html_record(), html_record(externalId="page-2", title="Other")])

    assert len(responses.calls) == 10
    assert [b["success"] for b in bookmarks(state)] == [False, False]
    assert state["summary"]["Documents"]["fail"] == 2
    assert API_KEY not in output


@pytest.mark.parametrize(
    "url",
    ["http://api.cloud.coveo.com/push/v1", "https://evil.example.com/push/v1"],
)
def test_push_api_url_must_be_https_coveo_host(url: str):
    with pytest.raises(ValueError, match="push_api_url"):
        TargetCoveo(config={**CONFIG, "push_api_url": url})


def test_required_config_is_enforced():
    with pytest.raises(ConfigValidationError):
        TargetCoveo(config={"api_key": API_KEY})


@responses.activate
def test_security_identity_user_alias_upsert():
    responses.put(IDENTITIES_URL, status=202)
    record = {
        "externalId": "confluence:user:acct-1",
        "identity": {"name": "acct-1", "type": "USER"},
        "mappings": [
            {
                "name": "user@example.test",
                "type": "USER",
                "provider": "Email Security Provider",
            }
        ],
    }

    state, output = run_target([record], stream="SecurityIdentities")

    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert request.method == "PUT"
    assert request.url == IDENTITIES_URL
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.body)
    assert body == {
        "identity": {"name": "acct-1", "type": "USER"},
        "mappings": [
            {
                "name": "user@example.test",
                "type": "USER",
                "provider": "Email Security Provider",
            }
        ],
    }
    [bookmark] = state["bookmarks"]["SecurityIdentities"]
    assert bookmark["success"] is True
    assert bookmark["externalId"] == "confluence:user:acct-1"
    assert bookmark["identity"] == "acct-1"
    assert API_KEY not in output


@responses.activate
def test_security_identity_group_full_replace():
    responses.put(IDENTITIES_URL, status=202)
    record = {
        "externalId": "confluence:group:eng",
        "identity": {"name": "eng", "type": "GROUP"},
        "members": [{"name": "acct-1", "type": "USER"}],
    }

    state, _ = run_target([record], stream="SecurityIdentities")

    body = json.loads(responses.calls[0].request.body)
    assert body["members"] == [{"name": "acct-1", "type": "USER"}]
    assert state["bookmarks"]["SecurityIdentities"][0]["success"] is True


@responses.activate
def test_security_identity_rejects_lowercase_type():
    responses.put(IDENTITIES_URL, status=202)
    state, _ = run_target(
        [
            {
                "externalId": "bad",
                "identity": {"name": "acct-1", "type": "User"},
            }
        ],
        stream="SecurityIdentities",
    )
    assert state["bookmarks"]["SecurityIdentities"][0]["success"] is False
    assert len(responses.calls) == 0
