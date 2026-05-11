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

## Source URL references

- GCIDE: https://gcide.gnu.org.ua/
- WordNet: https://wordnet.princeton.edu/
- FreeDict: https://freedict.org/
