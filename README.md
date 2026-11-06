# PHentai Downloader

A Windows app for downloading doujinshi and manga galleries to your PC and organising them
into CBZ files for comic readers.

> **Adults only (18+).** This tool downloads adult content. Respect each site's terms of
> service and the laws of your country.

![PHentai Downloader](docs/main.png)

## What it's for

- **Downloading galleries:** paste a gallery link and get every page saved, in full
  quality, as a numbered image folder.
- **Many sites:** built-in support for hentairead.com and nhentai.net. About 270 more
  sites work through [gallery-dl](https://github.com/mikf/gallery-dl), including e-hentai,
  hitomi, hentai2read, hentainexus, imhentai and mangadex. Other pages fall back to
  collecting their large images.
- **Downloading in bulk:** queue many links at once. An artist, tag or search page can be
  added as a whole and is split into its galleries. Unfinished downloads resume later.
- **Building CBZ files:** combine galleries into one CBZ for reading in apps like
  CDisplayEx, YACReader or Tachiyomi/Mihon. Each gallery becomes a numbered chapter
  inside. Set the order yourself, edit the CBZ later, or make one CBZ per color group of
  downloads.
- **Keeping a collection tidy:** color labels, search, duplicate detection, and file
  names that always fit Windows' path limit.

| Building a CBZ | One CBZ per color |
|---|---|
| ![Building a CBZ](docs/cbz_editor.png) | ![One CBZ per color](docs/cbz_by_color.png) |

## How to build

**You need:** Windows 10 or 11, Microsoft Edge (pre-installed) or Google Chrome, and
**Python 3.10 or newer** from <https://www.python.org/downloads/>. During Python setup,
tick **"Add python.exe to PATH"**.

1. Download this repository: **Code → Download ZIP**, then unzip it.
2. Double-click **`build.bat`**. The first build takes a few minutes while it installs the
   required libraries.
3. The finished program is **`dist\PHentaiDownloader4.exe`**. Copy it anywhere and create
   a shortcut.

**Run without building:** double-click **`run.bat`**. Or, from a terminal:

```bat
pip install -r requirements.txt
python phentai_downloader.py
```

## License

[GPL-2.0](LICENSE). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the libraries
used and their licenses.
