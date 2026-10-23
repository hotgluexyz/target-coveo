"""HTTP client and sink base classes for the Coveo Push API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from hotglue_etl_exceptions import InvalidCredentialsError, InvalidPayloadError
from hotglue_singer_sdk.target_sdk.auth import Authenticator
from hotglue_singer_sdk.target_sdk.client import HotglueBaseSink, HotglueSink

from target_coveo.validation import normalize_push_api_url

if TYPE_CHECKING:
    import requests

DEFAULT_PUSH_API_URL = "https://api.cloud.coveo.com/push/v1"
_MAX_ERROR_BODY_CHARS = 1000


class CoveoApiKeyAuthenticator(Authenticator):
    """Sends the Coveo API key as a bearer token."""

    @property
    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._config['api_key']}"}


class CoveoSink(HotglueBaseSink):
    """Coveo base sink: regional base URL, auth and Push API error mapping."""

    @property
    def base_url(self) -> str:
        return normalize_push_api_url(self.config.get("push_api_url"), DEFAULT_PUSH_API_URL)

    @property
    def authenticator(self) -> Any:
        return CoveoApiKeyAuthenticator(self._target)

    @property
    def default_headers(self) -> dict:
        # Build a fresh dict so auth headers never leak into shared class state.
        return {"Accept": "application/json", **self.authenticator.auth_headers}

    def documents_endpoint(self, document_id: str, **query: str) -> str:
        """Build the documents endpoint with every query value safely percent-encoded."""
        organization_id = quote(str(self.config["organization_id"]), safe="")
        source_id = quote(str(self.config["source_id"]), safe="")
        pairs = {"documentId": document_id, **query}
        query_string = "&".join(f"{key}={quote(value, safe='')}" for key, value in pairs.items())
        return f"/organizations/{organization_id}/sources/{source_id}/documents?{query_string}"

    def _security_provider_path(self, resource: str) -> str:
        organization_id = quote(str(self.config["organization_id"]), safe="")
        provider_id = quote(str(self.config["security_identity_provider_id"]), safe="")
        return f"/organizations/{organization_id}/providers/{provider_id}/{resource}"

    def security_identities_endpoint(self) -> str:
        """Groups/users without email aliases → Push API `/permissions`."""
        return self._security_provider_path("permissions")

    def security_identity_mappings_endpoint(self) -> str:
        """User aliases (Email Security Provider) → Push API `/mappings`."""
        return self._security_provider_path("mappings")

    def validate_response(self, response: requests.Response) -> None:
        """Map Coveo errors: 429/5xx retry; auth and payload errors fail the record."""
        status = response.status_code
        if status == 429 or status >= 500:  # noqa: PLR2004
            super().validate_response(response)
            return
        if status < 400:  # noqa: PLR2004
            return
        body = (response.text or "")[:_MAX_ERROR_BODY_CHARS]
        message = f"Coveo Push API returned HTTP {status}: {body}"
        if status in (401, 403):
            raise InvalidCredentialsError(message)
        raise InvalidPayloadError(message)


class CoveoRecordSink(CoveoSink, HotglueSink):
    """Coveo record sink base class."""

    name = "CoveoRecordSink"
