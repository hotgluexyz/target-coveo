"""Coveo target sink class, which handles writing streams."""

from __future__ import annotations

import copy
from typing import ClassVar

from target_coveo.client import CoveoRecordSink
from target_coveo.validation import (
    ACTION_KEY,
    CONTROL_KEYS,
    DOCUMENT_ID_KEY,
    EXTERNAL_ID_KEY,
    is_delete_record,
    validate_delete_record,
    validate_security_identity_record,
    validate_upsert_record,
)


class SecurityIdentitiesSink(CoveoRecordSink):
    """Upserts security identities into the configured Coveo identity provider."""

    name = "SecurityIdentities"
    allows_externalid: ClassVar[list[str]] = ["SecurityIdentities"]

    def preprocess_record(self, record: dict, context: dict) -> dict:  # noqa: ARG002
        record = copy.deepcopy(record)
        validate_security_identity_record(record)
        return record

    def upsert_record(self, record: dict, context: dict) -> tuple[str, bool, dict]:  # noqa: ARG002
        body = {key: value for key, value in record.items() if key != EXTERNAL_ID_KEY}
        response = self.request_api(
            "PUT",
            endpoint=self.security_identities_endpoint(),
            params={},
            request_data=body,
            headers={},
        )
        identity_name = record["identity"]["name"]
        self.logger.info(
            "Coveo accepted security identity externalId=%s name=%s (HTTP %s)",
            record.get(EXTERNAL_ID_KEY),
            identity_name,
            response.status_code,
        )
        return identity_name, True, {
            "action": "upsert",
            "httpStatus": response.status_code,
            "coveoStatus": "accepted",
            "identity": identity_name,
            "identityType": record["identity"]["type"],
        }


class DocumentsSink(CoveoRecordSink):
    """Pushes single documents to a Coveo Push source (idempotent PUT by documentId)."""

    name = "Documents"
    endpoint = "/organizations/{organization_id}/sources/{source_id}/documents"
    # Keep externalId in the record so preprocess can validate it; upsert strips it.
    allows_externalid: ClassVar[list[str]] = ["Documents"]

    def preprocess_record(self, record: dict, context: dict) -> dict:  # noqa: ARG002
        """Validate the record against the public contract without reshaping it."""
        record = copy.deepcopy(record)
        if is_delete_record(record):
            validate_delete_record(record)
        else:
            validate_upsert_record(record)
        return record

    def upsert_record(self, record: dict, context: dict) -> tuple[str, bool, dict]:  # noqa: ARG002
        """Send the already-validated record to Coveo; return documentId as the state id."""
        document_id = record[DOCUMENT_ID_KEY]

        if record.get(ACTION_KEY) is not None:
            response = self.request_api(
                "DELETE",
                endpoint=self.documents_endpoint(document_id, deleteChildren="false"),
                params={},
                headers={},
            )
            action = "delete"
        else:
            body = {key: value for key, value in record.items() if key not in CONTROL_KEYS}
            query = {}
            if "compressedBinaryData" in body:
                query["compressionType"] = body["compressionType"]
            response = self.request_api(
                "PUT",
                endpoint=self.documents_endpoint(document_id, **query),
                params={},
                request_data=body,
                headers={},
            )
            action = "upsert"

        state_updates = {
            "documentId": document_id,
            "action": action,
            "httpStatus": response.status_code,
            # A 2xx only means Coveo accepted the operation; indexing is asynchronous.
            "coveoStatus": "accepted",
            "indexed": False,
        }
        self.logger.info(
            "Coveo accepted %s for externalId=%s (HTTP %s, not yet indexed)",
            action,
            record.get(EXTERNAL_ID_KEY),
            response.status_code,
        )
        return document_id, True, state_updates
