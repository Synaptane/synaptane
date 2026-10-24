# Third-party notices

Synaptane is licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE`). It ships or
installs the third-party components listed below, each under its own license. This file
gathers the notices those licenses require; where a component already ships its full license
text, the file is referenced instead of being copied here.

Contents:

1. [Fonts](#1-fonts)
2. [Icons](#2-icons)
3. [Python dependencies](#3-python-dependencies)
4. [Native libraries bundled in Python wheels](#4-native-libraries-bundled-in-python-wheels)
5. [Docker image](#5-docker-image)
6. [nginx proxy image](#6-nginx-proxy-image)
7. [Regenerating the Python list](#7-regenerating-the-python-list)
8. [Test files](#8-test-files)

---

## 1. Fonts

The web interface embeds Latin subsets of the following typefaces, in
`src/synaptane/web/static/fonts/`. They are licensed under the
[SIL Open Font License, Version 1.1](https://openfontlicense.org) (`OFL-1.1`); the full license
text, with the upstream copyright line, sits next to the font files. None of these licenses
declares a Reserved Font Name.

| Family | Files | Copyright | License text |
|---|---|---|---|
| Geist, Geist Mono | `Geist-latin.woff2`, `GeistMono-latin.woff2` | Copyright 2024 The Geist Project Authors (https://github.com/vercel/geist-font) | `src/synaptane/web/static/fonts/OFL-Geist.txt` |
| Instrument Sans | `InstrumentSans-latin.woff2` | Copyright 2022 The Instrument Sans Project Authors (https://github.com/Instrument/instrument-sans) | `src/synaptane/web/static/fonts/OFL-InstrumentSans.txt` |
| JetBrains Mono | `JetBrainsMono-latin.woff2` | Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono) | `src/synaptane/web/static/fonts/OFL-JetBrainsMono.txt` |
| Newsreader (roman and italic) | `Newsreader-latin.woff2`, `Newsreader-italic-latin.woff2` | Copyright 2020 The Newsreader Project Authors (http://github.com/productiontype/Newsreader) | `src/synaptane/web/static/fonts/OFL-Newsreader.txt` |

The files are subsets (Latin character range, WOFF2) of the upstream variable fonts. They
keep the upstream copyright notice (`name` ID 0) and license URL (`name` ID 14).

## 2. Icons

The inline pictograms in `src/synaptane/web/templates/icons.html` are drawn on a 24×24 grid
with 2px rounded strokes. Several of them reuse or adapt path data from two icon sets:

- [Feather](https://github.com/feathericons/feather) (MIT): `shield`, `gear` (Feather
  `settings`), `inbox`, `logout` (`log-out`), `moon`, `warn` (`alert-triangle`), with
  coordinates rounded to one decimal.
- [Lucide](https://github.com/lucide-icons/lucide) (ISC; Lucide itself derives part of its set
  from Feather): `users`, `sun`, `check`, `x`.

The remaining pictograms, the file-type icons and the Synaptane mark are original to this
project.

### Lucide license

```text
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

### Feather license

```text
The MIT License (MIT)

Copyright (c) 2013-2023 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 3. Python dependencies

Runtime dependencies resolved in `uv.lock` (development tools excluded), as installed in the
Docker image and by an installation from source (`uv sync --no-dev`; Synaptane is not
published on PyPI). Versions are those pinned by the lock file at the time of writing;
licenses are read from each package's metadata and normalized to SPDX identifiers. Every
package ships its own license file in its `*.dist-info/` directory (in the Docker image:
`/app/.venv/lib/python3.12/site-packages/`).

| Package | Version | License (SPDX) |
|---|---|---|
| alembic | 1.20.0 | MIT |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anthropic | 2.0.0 | Apache-2.0 (vendored `httpx-aiohttp`: BSD-3-Clause, see below) |
| anyio | 4.15.1 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT |
| attrs | 26.1.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| docstring-parser | 0.18.0 | MIT |
| fastapi | 0.159.1 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx2 | 2.16.0 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jinja2 | 3.1.6 | BSD-3-Clause |
| jiter | 0.17.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mako | 1.4.3 | MIT |
| markdown-it-py | 4.2.0 | MIT |
| markupsafe | 3.0.4 | BSD-3-Clause |
| mcp | 2.3.0 | MIT |
| mcp-types | 2.3.0 | MIT |
| mdit-py-plugins | 0.6.1 | MIT |
| mdurl | 0.1.2 | MIT |
| nh3 | 0.3.7 | MIT |
| olefile | 0.47 | BSD-2-Clause (code derived from PIL: MIT-CMU) |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pgvector | 0.5.1 | MIT |
| pillow | 13.0.0 | MIT-CMU (bundled libraries: see section 4) |
| **psycopg** | 3.3.6 | **LGPL-3.0-only** |
| **psycopg-binary** | 3.3.6 | **LGPL-3.0-only** (bundled libraries: see section 4) |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic-core | 2.50.1 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| pyjwt | 2.15.1 | MIT |
| pypdfium2 | 5.14.0 | Apache-2.0 OR BSD-3-Clause (bundled PDFium: see section 4) |
| python-multipart | 0.0.32 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| sqlalchemy | 2.1.6 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| truststore | 0.10.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |

Locked but installed only on other platforms (environment markers), hence absent from the
Linux Docker image: `httpx2-jsfetch` 1.0 (Emscripten, BSD-3-Clause), `pywin32` 312 (Windows,
PSF-2.0), `tzdata` 2026.4 (Windows, Apache-2.0).

`anthropic` vendors part of [httpx-aiohttp](https://github.com/karpetrosyan/httpx-aiohttp)
0.2.0 (BSD-3-Clause, Copyright (c) 2025, Karen Petrosyan) in `anthropic/_vendor/httpx_aiohttp/`,
with its license text (`LICENSE`) and provenance (`NOTICE.md`) in that directory.

Compiled extensions (`cryptography`, `jiter`, `nh3`, `pydantic-core`, `rpds-py`) statically
include their own Rust or C dependencies; their upstream projects document those licenses
(MIT, Apache-2.0 or BSD for the most part; `cryptography` wheels also embed OpenSSL, Apache-2.0).

### psycopg (LGPL-3.0-only)

`psycopg` and `psycopg-binary` are licensed under the GNU Lesser General Public License,
version 3 only. Synaptane uses them **unmodified**, as separately installed packages that it
imports (dynamic linking): they are never copied into Synaptane's own source code. Their
license text ships in `psycopg-3.3.6.dist-info/licenses/LICENSE.txt` and
`psycopg_binary-3.3.6.dist-info/licenses/LICENSE.txt`; the GNU GPL version 3, which the LGPL
incorporates by reference, is available at <https://www.gnu.org/licenses/gpl-3.0.txt>. Their
source code is available at <https://github.com/psycopg/psycopg> and on PyPI
(<https://pypi.org/project/psycopg/>). Because they are installed as separate packages, users
can replace them with a modified or different build, for example by changing the version
locked in `uv.lock` and rebuilding the image.

## 4. Native libraries bundled in Python wheels

Some binary wheels embed native libraries. Their license texts ship with the wheels; the
paths below are relative to `site-packages/` (`/app/.venv/lib/python3.12/site-packages/` in
the Docker image).

### Pillow

Bundled in `pillow.libs/` or linked into `PIL/*.so`: FreeType (dual-licensed
`FTL OR GPL-2.0-or-later`, used here under the FreeType License), HarfBuzz (MIT), libraqm
(MIT), libjpeg-turbo (IJG and BSD-3-Clause), libpng (libpng-2.0), libtiff (libtiff), libwebp
and libsharpyuv (BSD-3-Clause), libavif (BSD-2-Clause) with its AV1 codecs, Little CMS 2
(MIT), OpenJPEG (BSD-2-Clause), Brotli (MIT), zlib-ng in its zlib-compatible build (Zlib),
liblzma (0BSD), Zstandard (BSD-3-Clause), libxcb and libXau (X11/MIT). Full texts:
`pillow-13.0.0.dist-info/licenses/LICENSE`; inventory: `pillow-13.0.0.dist-info/sboms/`.

FriBiDi (LGPL-2.1-or-later) is **not** bundled: libraqm loads it at run time, only if the
system provides it (in the Docker image, the Debian package `libfribidi0`, installed as a
dependency of Tesseract; see section 5).

### psycopg-binary

Bundled in `psycopg_binary.libs/`: libpq (PostgreSQL License), OpenSSL 3 `libssl` and
`libcrypto` (Apache-2.0), MIT Kerberos (`libkrb5`, `libgssapi_krb5`, `libk5crypto`,
`libkrb5support`, `libcom_err`; MIT-style licenses), OpenLDAP `libldap` and `liblber`
(OLDAP-2.8), Cyrus SASL `libsasl2` (BSD-style, CMU), PCRE (BSD-3-Clause), libselinux (public
domain), keyutils `libkeyutils` (LGPL-2.1-or-later) and `libcrypt` (LGPL-2.1-or-later). The wheel ships only the
LGPL text for psycopg itself; the origin of each bundled library (CentOS 7 packages) is listed
in `psycopg_binary-3.3.6.dist-info/sboms/auditwheel.cdx.json`.

This product includes software developed by Computing Services at Carnegie Mellon University
(http://www.cmu.edu/computing/).

### pypdfium2

`pypdfium2_raw/libpdfium.so` is a build of [PDFium](https://pdfium.googlesource.com/pdfium/)
(BSD-3-Clause) and its dependencies: Abseil (Apache-2.0), AGG 2.3, fast_float (MIT),
FreeType (FTL),
ICU (Unicode-3.0), Little CMS (MIT), libjpeg-turbo (IJG and BSD-3-Clause), OpenJPEG
(BSD-2-Clause), libpng, libtiff, LLVM libc (Apache-2.0 WITH LLVM-exception), simdutf and zlib.
Full texts: `pypdfium2-5.14.0.dist-info/licenses/data/linux_x64/BUILD_LICENSES/` (directory
named after the wheel's platform) and `pypdfium2-5.14.0.dist-info/licenses/LICENSES/`.

### Required acknowledgements

- Portions of this software are copyright © 2026 The FreeType Project (www.freetype.org).
  All rights reserved.
- This software is based in part on the work of the Independent JPEG Group.

## 5. Docker image

The image built from `Dockerfile` is based on the official Docker image
`python:3.12-slim-trixie` (Debian 13 "trixie" with CPython 3.12, PSF-2.0, and pip, MIT). On
top of it, the following Debian packages and their dependencies are installed with `apt`:

| Debian package | Main license |
|---|---|
| ca-certificates | MPL-2.0 (certificate data), GPL-2.0-or-later (scripts) |
| git | GPL-2.0-only |
| openssh-client | OpenSSH license (BSD-style) |
| ripgrep | MIT OR Unlicense |
| tini | MIT |
| tesseract-ocr, tesseract-ocr-fra, tesseract-ocr-eng | Apache-2.0 |
| catdoc | GPL-2.0-only (charset tables: Unicode license) |
| postgresql-client-17 | PostgreSQL License |
| age | BSD-3-Clause |
| rclone | MIT |

These programs run as separate executables; Synaptane does not link to them. Inside the
image:

- the license of every installed Debian package is in `/usr/share/doc/<package>/copyright`;
- the full texts of the common licenses those files refer to (GPL-2, GPL-3, LGPL-2.1,
  LGPL-3, Apache-2.0…) are in `/usr/share/common-licenses/`;
- Synaptane's own `LICENSE`, `NOTICE` and this file are in `/usr/share/doc/synaptane/`;
- the Python dependencies of section 3 live in `/app/.venv`.

**Source code.** The exact list of packages and versions of a published image is in its SBOM,
attested with the image (BuildKit SBOM and provenance, signed by GitHub):

```sh
docker buildx imagetools inspect ghcr.io/synaptane/synaptane:X.Y.Z --format '{{ json .SBOM }}'
gh attestation verify oci://ghcr.io/synaptane/synaptane:X.Y.Z -R synaptane/synaptane
```

The source code of those exact Debian versions, including the GPL and LGPL ones, remains
available from the Debian archive snapshots at <https://snapshot.debian.org/> (current
versions: <https://sources.debian.org/>, or `apt-get source <package>`).

### Optional agent CLIs (`AGENT_CLIS=true`)

When the image is built with `--build-arg AGENT_CLIS=true` (the `Dockerfile` default for local
builds), two command-line tools are added, copied from their npm packages:

- **Codex CLI** (`@openai/codex`), Apache-2.0, © OpenAI. Its vendored directory
  (`/opt/codex`) also contains ripgrep and other helper tools with their own licenses.
- **Claude Code** (`@anthropic-ai/claude-code`), **proprietary software**, © Anthropic PBC,
  subject to Anthropic's Commercial Terms of Service (or the terms that apply to your
  account). It is not open source and is not covered by Synaptane's license.

**The images published by the Synaptane project are built with `AGENT_CLIS=false`** (see
`.github/workflows/release.yml`) and contain neither of these tools. If you build an image
with `AGENT_CLIS=true`, you are responsible for complying with their terms, in particular
before redistributing that image.

## 6. nginx proxy image

The image `ghcr.io/synaptane/synaptane-nginx`, used only by the all-in-one mode
(`COMPOSE_PROFILES=proxy`), is built from `deploy/nginx/Dockerfile`. It is the official Docker
image `nginx:1.31-alpine` (nginx built by the nginx project on Alpine Linux), with the
`openssl` package added and Synaptane's nginx configuration template and start script
(Apache-2.0). Synaptane's `LICENSE`, `NOTICE` and this file are in `/usr/share/doc/synaptane/`
inside the image.

Main components and their licenses (as declared by the Alpine and nginx packages):

| Component | License |
|---|---|
| nginx and its modules (njs, ACME, GeoIP, image filter, XSLT) | BSD-2-Clause |
| Alpine Linux base (`alpine-baselayout`, `apk-tools`, `pax-utils`) | GPL-2.0-only |
| BusyBox | GPL-2.0-only |
| musl libc | MIT |
| OpenSSL | Apache-2.0 |
| PCRE2 | BSD-3-Clause |
| zlib | Zlib |
| libxml2 (MIT), libxslt (X11), GD (GD), FreeType (`FTL OR GPL-2.0-or-later`), GeoIP (LGPL-2.1-or-later), libgcc (GPL-3.0-or-later with the GCC Runtime Library Exception), gettext's `libintl` (LGPL-2.1-or-later) and the other libraries the modules need | their own licenses |

The complete list, with versions and licenses, is in the image's attested SBOM (see section 5)
or, inside the image, `apk list --installed`. Alpine packages do not ship license texts: they
are in the sources, available from Alpine at <https://gitlab.alpinelinux.org/alpine/aports>
(package recipes, by release branch) and <https://pkgs.alpinelinux.org/>; nginx's license is at
<https://nginx.org/LICENSE>, and the sources of the nginx packages at
<https://github.com/nginx/pkg-oss>.

## 7. Regenerating the Python list

The table in section 3 can be regenerated from `uv.lock` and the project environment:

```sh
scripts/third-party-licenses.sh
```

The script keeps only the runtime dependencies (`uv export --frozen --no-dev`), reports the
locked packages that are not installed on the current platform, and prints a Markdown table
with [pip-licenses](https://pypi.org/project/pip-licenses/)
(`uv run --frozen --with pip-licenses pip-licenses --from=mixed --format=markdown --packages ...`).
Licenses that a package declares only through trove classifiers come out as, for example,
`BSD License`; check its license file and use the SPDX identifier in this document.
Extra arguments go to pip-licenses, for example `--format=csv` or `--with-urls`.

When upgrading Pillow, psycopg-binary or pypdfium2, check their bundled libraries again
(section 4): `ls site-packages/*.libs` and the `sboms/` and `licenses/` directories of their
`*.dist-info`. When upgrading `anthropic`, check its `_vendor/` directory again.

## 8. Test files

Two PowerPoint files in `tests/fixtures/legacy/` (`poi-basic-test.ppt`,
`poi-password-protected.ppt`) come unchanged from the test data of
[Apache POI](https://poi.apache.org/), Copyright 2003-2026 The Apache Software Foundation,
licensed under the Apache License 2.0 (the license of Synaptane, see `LICENSE`). This product
includes software developed at The Apache Software Foundation (https://www.apache.org/).
They are only used by the tests and are not part of the Docker image; their origin is in
`tests/fixtures/legacy/README.md`.
