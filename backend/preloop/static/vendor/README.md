# Vendored API documentation assets

`/docs/api` (Swagger UI) and `/docs/redoc` (ReDoc) are served entirely from
the API origin. These files are checked in so the pages render on air-gapped
or egress-restricted installs, and so a strict Content-Security-Policy does not
have to allow a third-party CDN.

The versions match the ones this repository pinned before the assets were
vendored. They are served by a `/static` mount in
`backend/preloop/api/app.py`.

## Files

| File | Source | Version | License | SHA-256 |
| --- | --- | --- | --- | --- |
| `swagger-ui-bundle.js` | [swagger-ui-dist](https://www.npmjs.com/package/swagger-ui-dist) | 5.9.0 | Apache-2.0 | `2a556306524bed2ca668ec5ae19b1dbd4d9cdaa34795c9063a1c44b29a9c6097` |
| `swagger-ui.css` | [swagger-ui-dist](https://www.npmjs.com/package/swagger-ui-dist) | 5.9.0 | Apache-2.0 | `c24ecffd63fc797d37bed1c68ea030479ad1c7a30638ffb6b5a2559ea98bc431` |
| `redoc.standalone.js` | [redoc](https://www.npmjs.com/package/redoc) | 2.0.0 | MIT | `c7f107f5259486ec29f726db25e31a46a563b09f5209fd90c0371677e576d311` |
| `favicon.png` | Preloop console (`frontend/public/images/favicon.png`) | — | Project asset | `0a01f8f3990e9342359f4d8e789b81d4c85249cfdec359ff1a8b77624d31a10c` |

`SHA256SUMS` is the machine-readable manifest for the four served files;
`backend/tests/api/test_docs_self_contained.py` verifies it.

The `*.LICENSE` / `*.LICENSE.txt` / `*.NOTICE` files carry the upstream
notices required by the Apache-2.0 and MIT licenses. `swagger-ui.NOTICE` is
the upstream file; note that `swagger-ui-dist@5.9.0` does not ship the
`swagger-ui-bundle.js.LICENSE.txt` its banner refers to, so the package
`LICENSE` and `NOTICE` are vendored instead.

## Updating

1. Pick the new version and download each bundle from the CDN, e.g.
   `https://cdn.jsdelivr.net/npm/swagger-ui-dist@<version>/swagger-ui-bundle.js`.
2. Cross-check the download against a second registry (unpkg) before trusting
   it, then update `SHA256SUMS`.
3. Update the table above and the version comments in
   `backend/preloop/api/app.py`.
4. Refresh the matching license and notice files.
