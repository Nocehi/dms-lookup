# Dictionary sources

This project does not distribute dictionary data. Users are responsible for installing dictionaries whose licenses permit their intended use.

## Redistribution status

| Status | Meaning |
|--------|---------|
| **redistributable** | Included in public package repos; license permits redistribution |
| **user-installed only** | Available for download but license must be checked before repackaging |
| **do not redistribute** | License explicitly restricts redistribution; local use only |

## Currently supported dictionaries

| Dictionary | Package / source | License | Redistribute? | Words |
|------------|-----------------|---------|---------------|-------|
| dictd_www.dict.org_gcide | `stardict-dictd_www.dict.org_gcide` (Arch) | GPL-2 | redistributable | 174,222 |
| WordNet | `stardict-wordnet` (Arch, AUR) | WordNet License (permissive) | redistributable | 149,535 |
| 萌典重編國語辭典 | Manual install | CC / public (educational) | redistributable | 161,194 |
| XDICT英漢辭典 | Manual install | Unknown | user-installed only | 174,049 |
| XDICT漢英辭典 | Manual install | Unknown | user-installed only | 158,137 |
| reader.dict JA | Manual install | © reader.dict 2026 | user-installed only | 100,036 |
| words.hk 粵語詞典 | Manual convert from CSV | ALL RIGHTS RESERVED | **do not redistribute** | 16,204 |
| English-中文 FreeDict+WikDict | `stardict-freedict-eng-zho` | GPL | redistributable | 26,660 |
| JMdict (org.edrdg.jmdict-jpn-eng) | Manual install from koaeH/JMdict-sdcv | CC-BY-SA 4.0 | redistributable | 412,413 |
| CC-CEDICT 漢英辭典 | Manual convert from source | CC-BY-SA 4.0 | redistributable | 122,437 |
| wikt-en-en (English Wiktionary) | Manual install from dictinfo.com | CC-BY-SA 3.0 + GFDL | redistributable | 1,306,573 |

## Installation

### System dictionaries (Arch Linux)

```bash
sudo pacman -S sdcv stardict-dictd_www.dict.org_gcide stardict-freedict-eng-zho
```

WordNet may be in AUR:

```bash
yay -S stardict-wordnet
```

### User-scope dictionaries

```bash
mkdir -p ~/.local/share/stardict/dic
# Place .ifo, .idx*, .dict* files into ~/.local/share/stardict/dic/
sdcv --list-dicts
```

User-scope dictionaries take precedence over system ones.

## Adding new dictionaries

1. Download or convert a Stardict-compatible dictionary.
2. Place the `.ifo`, `.idx` (or `.idx.gz`), and `.dict` (or `.dict.dz`) files in `~/.local/share/stardict/dic/`.
3. Run `sdcv --list-dicts` to verify.
4. Update `~/.config/dank-lookup/config.json` with the new source name.

### CC-CEDICT (from source)

```bash
# Download latest CC-CEDICT source (tab-separated, 125k entries)
curl -sL 'https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz' \
  | gzip -d > /tmp/cedict_ts_utf8.txt

# Convert with pyglossary (or use included conversion script)
pipx run pyglossary /tmp/cedict_ts_utf8.txt ~/.local/share/stardict/dic/cc-cedict/
```

### JMdict (koaeH/JMdict-sdcv)

```bash
# Download from GitHub release
curl -sL 'https://github.com/koaeH/JMdict-sdcv/releases/download/20230101/org.edrdg.jmdict-jpn-eng.tar.gz' \
  | tar xz -C ~/.local/share/stardict/dic/
```

### English Wiktionary (dictinfo.com)

```bash
# Download English-English Wiktionary (1.3M entries, 45 MB)
curl -sL 'https://www.dictinfo.com/stardict/wikt-en-en-2025-10-05.7z' \
  -o /tmp/wikt-en-en.7z
7z x /tmp/wikt-en-en.7z -o~/.local/share/stardict/dic/wikt-en-en/
```

## Source URL references

- GCIDE: https://gcide.gnu.org.ua/
- WordNet: https://wordnet.princeton.edu/
- FreeDict: https://freedict.org/
- JMdict: https://github.com/koaeH/JMdict-sdcv (CC-BY-SA 4.0, 412k entries)
- CC-CEDICT: https://cc-cedict.org/wiki/ or https://www.mdbg.net/chinese/dictionary?page=cedict (CC-BY-SA 4.0)
- English Wiktionary (dictinfo.com): https://www.dictinfo.com/
