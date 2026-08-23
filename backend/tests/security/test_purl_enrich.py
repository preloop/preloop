"""Generic VCS PURL enrichment hook (not a scanner)."""

from __future__ import annotations

from preloop.security.purl_enrich import (
    enrich_components_with_generic_purl,
    generic_vcs_purl,
)


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
