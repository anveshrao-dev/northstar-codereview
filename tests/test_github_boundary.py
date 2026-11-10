"""Transport-independent GitHub boundary checks using fictional identities."""

import pytest
from pydantic import ValidationError

from northstar_codereview import github_boundary as github


def revision():
    return github.PullRequestRevision(repository="northstar/fictional", pr_number=3,
        head_sha="a" * 40, base_sha="b" * 40, comparison_base_sha="c" * 40)


def test_revision_distinguishes_target_head_and_comparison_baseline():
    ref = revision()
    assert ref.head_sha != ref.base_sha
    assert ref.base_sha != ref.comparison_base_sha


@pytest.mark.parametrize("changes", [{"head_sha": "main"}, {"repository": "https://example.test/repo"}, {"repository": "northstar/.."}, {"pr_number": 0}])
def test_rejects_unpinned_or_invalid_pr_identity(changes):
    with pytest.raises(ValidationError):
        github.PullRequestRevision(**(revision().model_dump() | changes))


def test_pages_make_missing_patches_and_truncation_explicit():
    page = github.ChangedFilesPage(revision=revision(), total_files=2, next_cursor="page-2", truncated=True,
        files=[github.ChangedFile(file_path="a.py", change="modified", patch=None, patch_complete=False)])
    assert page.next_cursor == "page-2"
    assert page.truncated is True
    assert page.files[0].patch_complete is False


@pytest.mark.parametrize("changes", [
    {"patch_complete": True, "patch": None},
    {"change": "renamed", "previous_path": None},
    {"change": "modified", "previous_path": "old.py"},
    {"file_path": "../outside.py"},
])
def test_changed_file_rejects_false_completeness_and_unsafe_identity(changes):
    with pytest.raises(ValidationError):
        github.ChangedFile(**(dict(file_path="a.py", change="modified", patch=None, patch_complete=False) | changes))


def test_pinned_file_content_retains_exact_bytes_and_source_side():
    content = github.PinnedFileContent(file_path="a.py", side="base", commit_sha="c" * 40, content=b"old\r\n")
    assert content.content == b"old\r\n"
    assert content.side == "base"


def test_page_rejects_duplicate_file_paths():
    item = dict(file_path="a.py", change="modified", patch=None, patch_complete=False)
    with pytest.raises(ValidationError):
        github.ChangedFilesPage(revision=revision(), total_files=2, files=[item, item])
