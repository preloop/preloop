"""PURL hook, MCP request handler, and opt-in helpers."""

from __future__ import annotations

import json

from preloop.security.mcp_server import handle_request
from preloop.security.opt_in import wants_preloop_mcp, wants_repo_audit
from preloop.security.purl_enrich import (
    enrich_components_with_generic_purl,
    generic_vcs_purl,
)
from preloop.security.upstream import upstream_divergence

from tests.security.conftest import SYNTHETIC_PASSWORD


class TestPurlEnrich:
    def test_generic_vcs_purl_shape(self):
        purl = generic_vcs_purl(
            "libexample",
            "https://git.example.com/example/libexample.git",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            version="1.2.3",
        )
        assert purl.startswith("pkg:generic/libexample@1.2.3?")
        assert "vcs_url=" in purl
        assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in purl

    def test_enriches_only_unidentified_components(self):
        components = [
            {
                "name": "openssl",
                "version": "3.0.13",
                "purl": "pkg:generic/openssl@3.0.13",
            },
            {"name": "unidentified-mod", "version": "0.1"},
        ]
        out = enrich_components_with_generic_purl(
            components,
            "https://git.example.com/example/firmware.git",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        assert out[0]["purl_enriched"] is False
        assert out[0]["purl"] == "pkg:generic/openssl@3.0.13"
        assert out[1]["purl_enriched"] is True
        assert out[1]["purl"].startswith("pkg:generic/unidentified-mod@0.1")


class TestOptIn:
    def test_empty_allowlists_start_nothing(self):
        assert wants_preloop_mcp([], []) is False
        assert wants_repo_audit([], []) is False

    def test_legacy_name_only_tools_opt_in_preloop(self):
        assert wants_preloop_mcp([], [{"name": "get_issue"}]) is True
        assert wants_repo_audit([], [{"name": "get_issue"}]) is False

    def test_repo_audit_tools_do_not_start_preloop(self):
        tools = [{"server_name": "repo-audit", "name": "secret_history_scan"}]
        assert wants_repo_audit(["repo-audit"], tools) is True
        assert wants_preloop_mcp(["repo-audit"], tools) is False


class TestMcpHandler:
    def test_lists_tools(self):
        reply = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert reply is not None
        names = [t["name"] for t in reply["result"]["tools"]]
        assert names == [
            "secret_history_scan",
            "repo_hygiene_walk",
            "ci_workflow_audit",
            "upstream_divergence",
        ]

    def test_secret_history_tool_call(self, synthetic_history_repo):
        reply = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "secret_history_scan",
                    "arguments": {"repo_path": str(synthetic_history_repo["repo"])},
                },
            }
        )
        assert reply is not None
        text = reply["result"]["content"][0]["text"]
        assert SYNTHETIC_PASSWORD not in text
        payload = json.loads(text)
        assert payload["tool"] == "secret_history_scan"
        assert payload["rows"]


class TestUpstreamDivergence:
    def test_requires_url(self, synthetic_history_repo):
        try:
            upstream_divergence(str(synthetic_history_repo["repo"]), "")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
