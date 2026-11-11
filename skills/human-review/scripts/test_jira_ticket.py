"""A Ludvig ticket is a Jira issue: found by key, read with the reader's own token, cached."""
from __future__ import annotations

import io
import json

import semcov


def test_a_key_is_found_in_the_branch_the_title_or_the_front_matter():
    assert semcov.jira_key("", "feat: ML-3944 rpa integration", "feature/x") == "ML-3944"
    assert semcov.jira_key("", "", "feature/POG-12-paging") == "POG-12"
    assert semcov.jira_key("see #25", "fix things", "feature/capego-events") is None


def test_adf_reads_as_paragraphs_headings_and_lists():
    adf = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Goal"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Send the payload."}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "only RPA activities"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "retry on 5xx"}]}]}]}]}
    assert semcov.adf_text(adf).strip() == (
        "## Goal\n\nSend the payload.\n\n- only RPA activities\n- retry on 5xx")


def test_the_issue_is_asked_once_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        assert req.get_header("Authorization").startswith("Basic ")
        return Resp(json.dumps({"fields": {
            "summary": "RPA integration with Advania", "created": "2026-11-01T10:00:00.000+0100",
            "reporter": {"displayName": "A. Person"},
            "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "When an RPA activity completes, send it."}]}]}}}).encode())
    monkeypatch.setattr(semcov.urllib.request, "urlopen", fake_urlopen)
    got = semcov._jira_issue("ML-3944", tmp_path)
    assert got["title"] == "ML-3944: RPA integration with Advania"
    assert got["url"] == "https://acme.atlassian.net/browse/ML-3944"
    assert got["body"] == "When an RPA activity completes, send it."
    assert got["author"] == "A. Person"
    again = semcov._jira_issue("ML-3944", tmp_path)
    assert again == got and len(calls) == 1, "a rebuild needs no network"


def test_without_credentials_it_says_so_and_returns_nothing(tmp_path, monkeypatch, capsys):
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(semcov, "JIRA_ENV", tmp_path / "absent.env")
    assert semcov._jira_issue("ML-1", tmp_path) is None
    assert "JIRA_API_TOKEN" in capsys.readouterr().err
