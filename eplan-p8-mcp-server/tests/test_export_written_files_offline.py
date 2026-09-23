"""
What the PDF/PXF export wrappers report about the files they wrote.

The defect these cover: EPLAN's export actions take a filename but let the
active export scheme decide the basename, and still return success. The
wrappers used to echo the requested EXPORTFILE back, so every signal a caller
had said the file was where it asked for it while the real output sat next to
it under another name.

No EPLAN needed - _execute_with_quiet_mode is replaced by a fake that writes
whatever file the scenario calls for and returns the action result EPLAN would
have returned.
"""

import inspect
import os

import pytest

from api.actions import _base, export_


ACTION_RESULT = {
    "executor": "action",
    "success": True,
    "parameters": {"TYPE": "PDFPAGESSCHEME", "PAGENAME1": "+X/1"},
}


@pytest.fixture
def fake_export(monkeypatch):
    """
    Install a fake action executor.

    *writes* is a list of (basename, content) that the "export" drops into
    *directory*. Returns the list that records the action strings.
    """
    calls = []

    def install(directory, writes, result=None):
        def fake(action):
            calls.append(action)
            for name, content in writes:
                with open(os.path.join(str(directory), name), "w") as handle:
                    handle.write(content)
            return dict(ACTION_RESULT if result is None else result)

        monkeypatch.setattr(_base, "_execute_with_quiet_mode", fake)
        return calls

    return install


# ---------------------------------------------------------------------------
# The reported case: a different name, and nothing saying so
# ---------------------------------------------------------------------------

def test_renamed_output_is_reported_not_the_request(tmp_path, fake_export):
    """
    The scheme names the file; the wrapper must say what is on disk.

    This is the measured case: one page asked for as ltest.pdf came out under
    a name built from the page's structure identifier and a counter.
    """
    requested = tmp_path / "ltest.pdf"
    fake_export(tmp_path, [("STRUCT-1.pdf", "%PDF-1.7")])

    result = export_.export_pdf_pages(str(requested), page_names=["+X/1"])

    assert result["success"] is True
    assert result["requestedFileWritten"] is False
    assert result["writtenFiles"] == [str(tmp_path / "STRUCT-1.pdf")]
    assert result["requestedFile"] == str(requested)
    assert "did not write the requested basename" in result["note"]
    # The note must not name export_scheme: export_pxf_project has no
    # such parameter and shares this text.
    assert "pass an explicit export_scheme" not in result["note"]


def test_honoured_request_carries_no_note(tmp_path, fake_export):
    requested = tmp_path / "ltest.pdf"
    fake_export(tmp_path, [("ltest.pdf", "%PDF-1.7")])

    result = export_.export_pdf_pages(str(requested), page_names=["+X/1"])

    assert result["requestedFileWritten"] is True
    assert result["writtenFiles"] == [str(requested)]
    assert "note" not in result


# ---------------------------------------------------------------------------
# Why the snapshot carries mtime and size
# ---------------------------------------------------------------------------

def test_overwriting_an_existing_file_still_counts_as_written(tmp_path,
                                                              fake_export):
    """
    A name-only diff would report that nothing was written.

    Exporting the same page twice is the normal case, not the corner one, and
    the second run overwrites a file that was already there. Pinned because
    the cheap implementation - compare the set of names - is silently wrong
    exactly here, and wrong in the direction that reads as "the export
    produced nothing".
    """
    requested = tmp_path / "ltest.pdf"
    stale = tmp_path / "STRUCT-1.pdf"
    stale.write_text("older and shorter")
    fake_export(tmp_path,
                [("STRUCT-1.pdf", "%PDF-1.7 rewritten, a different size")])

    result = export_.export_pdf_pages(str(requested), page_names=["+X/1"])

    assert result["writtenFiles"] == [str(stale)]
    assert result["requestedFileWritten"] is False


def test_untouched_neighbours_are_not_claimed_as_output(tmp_path, fake_export):
    (tmp_path / "unrelated.pdf").write_text("not ours")
    fake_export(tmp_path, [("STRUCT-1.pdf", "%PDF-1.7")])

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result["writtenFiles"] == [str(tmp_path / "STRUCT-1.pdf")]


def test_success_with_nothing_written_says_so(tmp_path, fake_export):
    """success:true over an empty directory is the worst case, so name it."""
    fake_export(tmp_path, [])

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result["success"] is True
    assert result["writtenFiles"] == []
    assert result["requestedFileWritten"] is False
    assert "nothing in the target directory changed" in result["note"]


# ---------------------------------------------------------------------------
# Verification must never break a working export
# ---------------------------------------------------------------------------

def test_unlistable_directory_reports_unavailable_not_failure(tmp_path,
                                                              fake_export,
                                                              monkeypatch):
    """
    The server need not share a filesystem with EPLAN.

    When the directory cannot be read the honest answer is "unknown", never
    "nothing was written" - and certainly not an exception out of an export
    that succeeded.
    """
    fake_export(tmp_path, [("STRUCT-1.pdf", "%PDF-1.7")])
    monkeypatch.setattr(_base, "_snapshot_dir", lambda directory: None)

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result["success"] is True
    assert result["verification"].startswith("unavailable:")
    assert "writtenFiles" not in result
    assert "requestedFileWritten" not in result


def test_missing_directory_is_not_an_error(tmp_path, fake_export):
    """A first export into a directory that does not exist yet."""
    target = tmp_path / "not-there-yet"
    fake_export(tmp_path, [])

    result = export_.export_pdf_pages(str(target / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result["success"] is True
    assert result["verification"].startswith("unavailable:")


def test_crowded_directory_is_not_diffed(tmp_path, fake_export, monkeypatch):
    """
    The diff is O(files-in-dir) twice per export; above the cap it is skipped
    and reported as unavailable, never as "nothing was written".
    """
    monkeypatch.setattr(_base, "_SNAPSHOT_MAX_FILES", 3)
    for index in range(5):
        (tmp_path / ("old-%d.pdf" % index)).write_text("stale")
    fake_export(tmp_path, [("STRUCT-1.pdf", "%PDF-1.7")])
    scans = []
    real_snapshot = _base._snapshot_dir
    monkeypatch.setattr(
        _base, "_snapshot_dir",
        lambda directory: scans.append(directory) or real_snapshot(directory))

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result["success"] is True
    assert result["verification"].startswith("unavailable:")
    assert "more than 3 files" in result["verification"]
    assert "writtenFiles" not in result
    assert "requestedFileWritten" not in result
    assert len(scans) == 1


def test_directory_at_the_cap_is_still_diffed(tmp_path, fake_export,
                                              monkeypatch):
    monkeypatch.setattr(_base, "_SNAPSHOT_MAX_FILES", 3)
    for index in range(2):
        (tmp_path / ("old-%d.pdf" % index)).write_text("stale")
    fake_export(tmp_path, [("STRUCT-1.pdf", "%PDF-1.7")])

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result["writtenFiles"] == [str(tmp_path / "STRUCT-1.pdf")]


def test_a_failed_action_is_passed_through_untouched(tmp_path, fake_export):
    """Nothing to verify, and the failure must not be dressed up."""
    failure = {"executor": "action", "success": False, "message": "boom"}
    fake_export(tmp_path, [], result=failure)

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    assert result == failure


def test_existing_fields_are_preserved_verbatim(tmp_path, fake_export):
    """The #28 rule: add evidence, never rewrite what callers already match."""
    fake_export(tmp_path, [("STRUCT-1.pdf", "%PDF-1.7")])

    result = export_.export_pdf_pages(str(tmp_path / "ltest.pdf"),
                                      page_names=["+X/1"])

    for key, value in ACTION_RESULT.items():
        assert result[key] == value


# ---------------------------------------------------------------------------
# Coverage: which wrappers can carry this at all
# ---------------------------------------------------------------------------

def test_pdf_project_and_pxf_project_verify_too(tmp_path, fake_export):
    for func in (export_.export_pdf_project, export_.export_pxf_project):
        fake_export(tmp_path, [("renamed.out", "written by " + func.__name__)])
        result = func(str(tmp_path / "asked-for.out"))
        assert result["requestedFileWritten"] is False, func.__name__
        assert result["writtenFiles"] == [str(tmp_path / "renamed.out")], \
            func.__name__
        os.remove(str(tmp_path / "renamed.out"))


def test_only_the_export_file_wrappers_can_be_verified_this_way():
    """
    The issue asked whether the sibling exporters share the defect. They
    cannot in this form: dxf/dwg/graphics/3d take a DESTINATIONPATH, a
    directory, so there is no requested basename for a scheme to override.
    Pinned so that a later signature change is noticed rather than quietly
    leaving a wrapper out of the verification.
    """
    takes_export_file = sorted(
        name for name in dir(export_)
        if name.startswith("export_")
        and callable(getattr(export_, name))
        and "export_file" in inspect.signature(
            getattr(export_, name)).parameters
    )
    assert takes_export_file == ["export_pdf_pages", "export_pdf_project",
                                 "export_pxf_project"]
