# Third-party software

PHentai Downloader is licensed under the GNU General Public License v2 (see `LICENSE`).
It uses the libraries below. They are **not** included in this repository; they are
installed by `pip` (and packed into the `.exe` when you run `build.bat`).

| Library | License | Used for |
|---|---|---|
| [gallery-dl](https://github.com/mikf/gallery-dl) | GPL-2.0-only | Support for ~270 extra sites |
| [PySide6 / Qt for Python](https://doc.qt.io/qtforpython/) | LGPL-3.0 / GPL-2.0 / GPL-3.0 (used under GPL-2.0) | User interface |
| [Playwright for Python](https://github.com/microsoft/playwright-python) | Apache-2.0 | Controlling the local Edge/Chrome browser |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | MIT (bundles libcurl and BoringSSL under their own permissive licenses) | Fast HTTP downloads |
| [requests](https://github.com/psf/requests) (via gallery-dl) | Apache-2.0 | HTTP for gallery-dl |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0 with bootloader exception | Building the `.exe` (build tool only) |
| [Python](https://www.python.org/) | PSF License | Runtime |

## Credits

* The hentairead.com full-size image address pattern was learned from
  [hentairead-js](https://github.com/FireStreaker2/hentairead-js) (MIT License,
  © 2023 FireStreaker2). No code from that project is included.
* The nhentai.net API endpoints are the site's public API, as also used by gallery-dl.
  No gallery-dl code is copied; gallery-dl is used as a normal library.
* The app icon was created for this project (the "P" is drawn with the DejaVu Sans Bold
  font, whose license allows this).
