# Old binary Office files (test fixtures)

Files in the Office 97-2003 formats, read by `src/synaptane/extract.py` (catdoc for `.doc` and
`.xls`) and `src/synaptane/extract_ppt.py` (`.ppt`). Tests: `tests/unit/test_extract_legacy.py`,
`tests/unit/test_extract_ppt.py`, `tests/integration/test_legacy_office.py`.

## Made by this project

Written by `scripts/legacy_files.py` (python-docx, xlsxwriter and python-pptx, then converted by
LibreOffice in a throwaway Debian container) and kept as LibreOffice writes them. Their content
is fictional, the author is "Acme IT team" and they carry no personal data. They are licensed
like the rest of Synaptane (Apache-2.0).

| File | Witness words |
|---|---|
| `printer-memo.doc` | Zanzibar |
| `spare-parts.xls` | Okavango |
| `kickoff-slides.ppt` | Kilimanjaro (title), Serengeti (free text box), Kalahari (table), Zambezi (speaker notes) |

LibreOffice writes all the text of a `.ppt` in the drawing of the slides, none in the outline
where PowerPoint keeps titles and bodies.

## From Apache POI

Two files saved by Microsoft PowerPoint, from the test data of
[Apache POI](https://poi.apache.org/) (`test-data/slideshow/` in
<https://github.com/apache/poi>, copied unchanged from the `trunk` branch on 2026-10-11, commit
`f3688f45b0de`). Apache POI, Copyright 2003-2026 The Apache Software Foundation, licensed under
the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0), the license of Synaptane
(see `LICENSE` at the root of this repository); this product includes software developed at
The Apache Software Foundation (<https://www.apache.org/>).

| File | Original name | What it holds |
|---|---|---|
| `poi-basic-test.ppt` | `basic_test_ppt_file.ppt` | Two slides whose titles and bodies are in the outline, as PowerPoint writes them, and speaker notes |
| `poi-password-protected.ppt` | `Password_Protected-hello.ppt` | A presentation encrypted with a password |
