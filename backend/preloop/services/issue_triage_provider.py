"""Scoped provider operations for issue text and complexity labels."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote, urlparse

from preloop.schemas.issue_triage import TriageIssue
from preloop.sync.exceptions import TrackerError

CATALOGUE_PAGE_SIZE = 100
CATALOGUE_MAX_PAGES = 10
STANDARD_LABEL_COLORS = {
    "complexity:low": "0e8a16",
    "complexity:medium": "fbca04",
    "complexity:high": "b60205",
}


class TriageProviderError(ValueError):
    """Provider scope, response or operation cannot be safely established."""


class IssueTriageProvider:
    """Use an already authorized tracker client; never accept external URLs."""

    def __init__(self, client: Any, number: str) -> None:
        if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]*", number):
            raise TriageProviderError("invalid_issue_number")
        self.client = client
        self.number = number
        self.kind = client.tracker_type
        config = client.connection_details
        if not isinstance(config, dict):
            raise TriageProviderError("invalid_tracker_scope")
        if self.kind == "github":
            owner, repo = config.get("owner"), config.get("repo")
            if not all(
                isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9_.-]+", value)
                and value not in {".", ".."}
                for value in (owner, repo)
            ):
                raise TriageProviderError("invalid_repository_scope")
            self.repository = f"{owner}/{repo}"
            self.root = f"/repos/{self.repository}"
            web_url = getattr(client, "API_BASE_URL", "https://api.github.com")
            if web_url == "https://api.github.com":
                web_url = "https://github.com"
        elif self.kind == "gitlab":
            project = config.get("project_id")
            if isinstance(project, bool) or not isinstance(project, (str, int)):
                raise TriageProviderError("invalid_project_scope")
            self.project = str(project)
            if any(
                part in {".", ".."} for part in self.project.split("/")
            ) or not re.fullmatch(
                r"[1-9][0-9]*|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", self.project
            ):
                raise TriageProviderError("invalid_project_scope")
            self.root = f"/projects/{quote(self.project, safe='')}"
            web_url = (
                getattr(client, "url", None)
                or config.get("url")
                or "https://gitlab.com"
            )
        else:
            raise TriageProviderError("unsupported_triage_tracker")
        self.web_origin = urlparse(web_url).netloc.lower()
        self.web_prefix = urlparse(web_url).path.rstrip("/")
        if self.kind == "gitlab" and self.web_prefix.endswith("/api/v4"):
            self.web_prefix = self.web_prefix[:-7]
        self.issue_path = f"{self.root}/issues/{number}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            if self.kind == "github":
                return await self.client._request(
                    method, path, data=data, params=params
                )
            request = getattr(self.client.gl, f"http_{method.lower()}")
            kwargs: dict[str, Any] = {}
            if params is not None:
                kwargs["query_data"] = params
            if data is not None:
                kwargs["post_data"] = data
            return await self.client._make_request(request, path, **kwargs)
        except TrackerError as exc:
            # Provider errors can contain response bodies; expose only a code.
            raise TriageProviderError("triage_provider_request_failed") from exc

    async def read_issue(self) -> TriageIssue:
        """Fetch and validate authoritative identity, text, labels and revision."""
        raw = await self._request("GET", self.issue_path)
        if not isinstance(raw, dict):
            raise TriageProviderError("invalid_issue_response")
        identity = raw.get("number" if self.kind == "github" else "iid")
        if isinstance(identity, bool) or str(identity) != self.number:
            raise TriageProviderError("issue_identity_mismatch")
        if self.kind == "github" and "pull_request" in raw:
            raise TriageProviderError("triage_target_is_pull_request")
        if self.kind == "gitlab" and self.project.isdecimal():
            if str(raw.get("project_id")) != self.project:
                raise TriageProviderError("issue_project_mismatch")
        title = raw.get("title")
        body = raw.get("body" if self.kind == "github" else "description")
        state = raw.get("state")
        url = raw.get("html_url" if self.kind == "github" else "web_url")
        updated = raw.get("updated_at")
        if not isinstance(title, str) or not isinstance(body, (str, type(None))):
            raise TriageProviderError("invalid_issue_content")
        if not isinstance(state, str) or state not in {"open", "opened", "closed"}:
            raise TriageProviderError("invalid_issue_state")
        if not isinstance(url, str) or not isinstance(updated, (str, type(None))):
            raise TriageProviderError("invalid_issue_metadata")
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise TriageProviderError("invalid_issue_url") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.netloc.lower() != self.web_origin
        ):
            raise TriageProviderError("invalid_issue_url")
        path = unquote(parsed.path).rstrip("/")
        if self.kind == "github":
            if path.lower() != f"/{self.repository}/issues/{self.number}".lower():
                raise TriageProviderError("issue_repository_mismatch")
        elif not path.endswith(f"/-/issues/{self.number}"):
            raise TriageProviderError("issue_repository_mismatch")
        elif (
            not self.project.isdecimal()
            and path != f"{self.web_prefix}/{self.project}/-/issues/{self.number}"
        ):
            raise TriageProviderError("issue_project_mismatch")
        labels = raw.get("labels")
        if not isinstance(labels, list):
            raise TriageProviderError("invalid_issue_labels")
        if self.kind == "github":
            if any(not isinstance(label, dict) for label in labels):
                raise TriageProviderError("invalid_issue_labels")
            labels = [label.get("name") for label in labels]
        if any(not isinstance(label, str) or not label for label in labels):
            raise TriageProviderError("invalid_issue_labels")
        return TriageIssue(
            title=title,
            body=body or "",
            state="open" if state == "opened" else state,
            url=url,
            labels=labels,
            updated_at=updated,
        )

    async def catalogue(self) -> list[dict[str, str]]:
        """Return a complete bounded label catalogue, never a truncated prefix."""
        labels: list[dict[str, str]] = []
        seen: set[str] = set()
        for page in range(1, CATALOGUE_MAX_PAGES + 1):
            params: dict[str, Any] = {"page": page, "per_page": CATALOGUE_PAGE_SIZE}
            if self.kind == "gitlab":
                params["include_ancestor_groups"] = True
            batch = await self._request("GET", f"{self.root}/labels", params=params)
            if not isinstance(batch, list) or len(batch) > CATALOGUE_PAGE_SIZE:
                raise TriageProviderError("invalid_label_catalogue")
            for label in batch:
                if not isinstance(label, dict):
                    raise TriageProviderError("invalid_label_catalogue")
                name, description = label.get("name"), label.get("description")
                if not isinstance(name, str) or not name or name in seen:
                    raise TriageProviderError("ambiguous_label_catalogue")
                if not isinstance(description, (str, type(None))):
                    raise TriageProviderError("invalid_label_catalogue")
                seen.add(name)
                labels.append({"name": name, "description": description or ""})
            if len(batch) < CATALOGUE_PAGE_SIZE:
                return labels
        raise TriageProviderError("label_catalogue_limit_exceeded")

    async def create_label(self, name: str) -> None:
        """Create only the standard complexity family with managed descriptions."""
        if name not in STANDARD_LABEL_COLORS:
            raise TriageProviderError("invalid_standard_complexity_label")
        description = f"Preloop issue complexity: {name.split(':')[1]}"
        color = STANDARD_LABEL_COLORS[name]
        if self.kind == "gitlab":
            color = f"#{color}"
        await self._request(
            "POST",
            f"{self.root}/labels",
            data={
                "name": name,
                "description": description,
                "color": color,
            },
        )

    async def write_content(self, title: str, body: str) -> None:
        """Write title/body only; scope freshness is checked by the caller."""
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
            raise TriageProviderError("invalid_triage_content")
        key = "body" if self.kind == "github" else "description"
        await self._request(
            "PATCH" if self.kind == "github" else "PUT",
            self.issue_path,
            data={"title": title, key: body},
        )

    async def update_labels(self, add: list[str], remove: list[str]) -> None:
        """Apply deltas only; never replace unrelated human labels."""
        if any(not isinstance(value, list) for value in (add, remove)) or any(
            not isinstance(name, str)
            or not name
            or (self.kind == "gitlab" and "," in name)
            for name in [*add, *remove]
        ):
            raise TriageProviderError("invalid_label_delta")
        if set(add) & set(remove):
            raise TriageProviderError("overlapping_label_delta")
        add, remove = list(dict.fromkeys(add)), list(dict.fromkeys(remove))
        if not add and not remove:
            return
        if self.kind == "gitlab":
            data: dict[str, Any] = {}
            if add:
                data["add_labels"] = ",".join(add)
            if remove:
                data["remove_labels"] = ",".join(remove)
            await self._request("PUT", self.issue_path, data=data)
            return
        # Add first: a failed addition must not erase the previous assessment.
        if add:
            await self._request(
                "POST", f"{self.issue_path}/labels", data={"labels": add}
            )
        for name in remove:
            await self._request(
                "DELETE", f"{self.issue_path}/labels/{quote(name, safe='')}"
            )
