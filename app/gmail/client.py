"""Gmail access. `GmailApi` is the small surface the engine needs; `GoogleGmail` implements it."""

import logging
from typing import Any, Protocol

log = logging.getLogger("agent.gmail")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailAuthError(Exception):
    """Refresh token revoked or expired (invalid_grant)."""


class GmailUnavailable(Exception):
    """Transient failure: network, 5xx, rate limit. Safe to retry a read."""


class GmailSendUnknown(Exception):
    """The send request may or may not have been accepted (timeout after submission)."""


class GmailSendRejected(Exception):
    """Gmail definitely refused the send (4xx before acceptance)."""


class GmailApi(Protocol):
    def get_thread(self, thread_id: str) -> dict[str, Any]: ...

    def search_threads(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...

    def send(self, raw: str, thread_id: str | None) -> dict[str, Any]: ...

    def find_by_message_id(self, rfc_message_id: str) -> dict[str, Any] | None: ...


class GoogleGmail:
    """Real Gmail API client for one user, built from their refresh token."""

    def __init__(self, refresh_token: str, client_id: str, client_secret: str) -> None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _call(self, request, *, sending: bool = False):
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=0 if sending else 2)
        except RefreshError as exc:
            raise GmailAuthError(str(exc)) from exc
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status == 401:
                raise GmailAuthError(str(exc)) from exc
            if sending and 400 <= status < 500 and status != 429:
                raise GmailSendRejected(f"Gmail refused the send ({status})") from exc
            if sending:
                raise GmailSendUnknown(f"Gmail send failed ambiguously ({status})") from exc
            raise GmailUnavailable(f"Gmail error {status}") from exc
        except (TransportError, TimeoutError, OSError) as exc:
            if sending:
                raise GmailSendUnknown(str(exc)) from exc
            raise GmailUnavailable(str(exc)) from exc

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self._call(self._svc.users().threads().get(userId="me", id=thread_id, format="full"))

    def search_threads(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        res = self._call(self._svc.users().threads().list(userId="me", q=query, maxResults=limit))
        out = []
        for t in res.get("threads", []):
            meta = self._call(
                self._svc.users()
                .threads()
                .get(
                    userId="me",
                    id=t["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
            )
            out.append(meta)
        return out

    def send(self, raw: str, thread_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self._call(self._svc.users().messages().send(userId="me", body=body), sending=True)

    def find_by_message_id(self, rfc_message_id: str) -> dict[str, Any] | None:
        query = f"rfc822msgid:{rfc_message_id.strip('<>')} in:sent"
        res = self._call(self._svc.users().messages().list(userId="me", q=query, maxResults=1))
        msgs = res.get("messages", [])
        return msgs[0] if msgs else None
