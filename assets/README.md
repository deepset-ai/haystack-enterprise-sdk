# Brand assets

Logos for the Haystack Enterprise SDK.

| File | Use |
| --- | --- |
| `logo.svg` | Product logo, navy `#071233`. README hero (light) and the docs landing page. |
| `logo-dark.svg` | Same logo in white, for dark backgrounds (GitHub dark theme). |

`docs/_images/` holds a second copy of both, plus `logo-header.svg` (white wordmark on the navy MkDocs
header bar) and `favicon.svg`.

**The duplication is deliberate — do not "deduplicate" it.** `README.md` is rendered by GitHub *and* by
PyPI, and PyPI does not resolve repository-relative paths, so the README must reference these files by
absolute `raw.githubusercontent.com` URL. `docs/index.md` is rendered by MkDocs, which needs a
site-relative path so the page builds and serves without network access. The two engines cannot share
one reference.

## Colours

| Token | Hex |
| --- | --- |
| Brand navy | `#071233` |
| Electric blue | `#2558ff` |

`#2558ff` on white is 5.36:1 (WCAG AA for body text). White on `#071233` is 18.4:1. `#2558ff` on
`#071233` is only 3.43:1, so electric blue must not be used as a text or link colour on navy.

## Provenance

`logo.svg`, `logo-header.svg` and `favicon.svg` are official deepset brand files. `logo-dark.svg` is
derived from `logo.svg` by recolouring `#071233` to `#fff` — the same transformation brand applies for
the white variants of sibling products. Replace it if brand issues an official white SDK logo.

## Licence

These marks are trademarks of deepset. They are **not** covered by the Apache-2.0 licence that covers
the code in this repository; see section 6 of [`LICENSE`](../LICENSE).
