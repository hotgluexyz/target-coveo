"""Coveo target class."""

from __future__ import annotations

from typing import Any

from hotglue_singer_sdk import typing as th
from hotglue_singer_sdk.target_sdk.target import TargetHotglue

from target_coveo.client import DEFAULT_PUSH_API_URL
from target_coveo.sinks import DocumentsSink, SecurityIdentitiesSink
from target_coveo.validation import normalize_push_api_url


class TargetCoveo(TargetHotglue):
    """Target for the Coveo Push API."""

    name = "target-coveo"
    # Identities before documents so ACL principals resolve before secured content.
    SINK_TYPES = [SecurityIdentitiesSink, DocumentsSink]

    config_jsonschema = th.PropertiesList(
        th.Property(
            "api_key",
            th.StringType,
            required=True,
            description="Coveo API key with Push source edit privileges (sent as Bearer token)",
        ),
        th.Property(
            "organization_id",
            th.StringType,
            required=True,
            description="Coveo organization ID",
        ),
        th.Property(
            "source_id",
            th.StringType,
            required=True,
            description="Coveo Push source ID",
        ),
        th.Property(
            "security_identity_provider_id",
            th.StringType,
            required=True,
            description=(
                "Security identity provider ID for the secured Push source "
                "(e.g. Northstar Confluence Demo SIP)"
            ),
        ),
        th.Property(
            "push_api_url",
            th.StringType,
            description=(
                "Regional Push API base URL, e.g. https://api-eu.cloud.coveo.com/push/v1. "
                f"Defaults to {DEFAULT_PUSH_API_URL}"
            ),
        ),
    ).to_dict()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the target and validate the regional Push API URL early."""
        super().__init__(*args, **kwargs)
        if self.config:
            normalize_push_api_url(self.config.get("push_api_url"), DEFAULT_PUSH_API_URL)

    def get_record_id(
        self,
        sink_name: str,  # noqa: ARG002
        record: dict,
        relation_fields: list | None = None,  # noqa: ARG002
    ) -> dict:
        """Disable snapshot id injection: documentId is the only Coveo identity."""
        return record


if __name__ == "__main__":
    TargetCoveo.cli()
