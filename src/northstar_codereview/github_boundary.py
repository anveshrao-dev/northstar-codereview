"""Future GitHub ingestion contracts; no client, credentials, or networking.

A future adapter must pin every page/content response to PullRequestRevision,
exhaust pagination, and surface truncated or missing patches. A final page alone
does not prove that a diff is complete. Never follow arbitrary patch/content URLs.
"""

from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from .models import Contract, FilePath, PositiveInteger, Repository, ReviewRequest, Revision, SourceSide, Text


class PullRequestRevision(Contract):
    repository: Repository = Field(description="Authorized owner/repository identity.")
    pr_number: PositiveInteger
    head_sha: Revision = Field(description="Pinned pull-request head commit.")
    base_sha: Revision = Field(description="Pinned target-branch head commit.")
    comparison_base_sha: Revision = Field(description="Pinned comparison baseline, typically the merge-base.")

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        ReviewRequest(mode="github", repository=self.repository, pr_number=self.pr_number)
        return self


class ChangedFile(Contract):
    file_path: FilePath
    previous_path: FilePath | None = None
    change: Literal["added", "modified", "removed", "renamed"]
    patch: str | None = Field(default=None, max_length=1024 * 1024)
    patch_complete: bool = Field(default=False, strict=True, description="False for omitted, unavailable, or truncated patches.")

    @model_validator(mode="after")
    def check_patch(self) -> Self:
        if self.patch_complete and self.patch is None:
            raise ValueError("An absent patch cannot be declared complete")
        if (self.change == "renamed") != (self.previous_path is not None):
            raise ValueError("Only renamed files require a previous path")
        return self


class ChangedFilesPage(Contract):
    revision: PullRequestRevision
    files: tuple[ChangedFile, ...] = Field(max_length=100)
    total_files: int = Field(strict=True, ge=0, description="Server-reported total; compare with accumulated unique files.")
    next_cursor: Text | None = Field(default=None, max_length=256, description="Opaque continuation token, never an executable URL.")
    truncated: bool = Field(default=False, strict=True, description="True when the adapter cannot establish complete pagination/data.")

    @model_validator(mode="after")
    def check_file_identity(self) -> Self:
        paths = [file.file_path for file in self.files]
        if len(paths) != len(set(paths)) or len(paths) > self.total_files:
            raise ValueError("Page contains duplicate files or exceeds the reported total")
        return self


class PinnedFileContent(Contract):
    file_path: FilePath
    side: SourceSide
    commit_sha: Revision = Field(description="Must match the requested pinned head or comparison baseline.")
    content: bytes = Field(strict=True, max_length=1024 * 1024, description="Bounded transport bytes; still require text validation and hashing.")


class GitHubSourceAdapter(Protocol):
    """An interface only: callers must authorize repository scope separately.

    Future ingestion maps approved file bytes to the same path/side/content-hash
    manifest and in-memory source bundle used by local inspection. This protocol
    neither implements HTTP nor vouches for an adapter's returned source data.
    """

    def get_revision(self, repository: str, pr_number: int) -> PullRequestRevision:
        """Resolve authorized PR metadata to pinned commits."""
        ...

    def list_changed_files(self, revision: PullRequestRevision, *, cursor: str | None = None) -> ChangedFilesPage:
        """Return one bounded page; callers reconcile pagination and completeness."""
        ...

    def get_file(self, revision: PullRequestRevision, *, file_path: str, side: SourceSide) -> PinnedFileContent:
        """Retrieve bounded content at the supplied pinned source-side commit."""
        ...
