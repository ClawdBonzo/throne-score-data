# Throne Score restroom data

Offline restroom tiles used by the [Throne Score](https://thronescore.app) app.

- `v1/tiles/<lat>_<lng>.json` — every restroom record in the 1°×1° cell whose south-west corner is (lat, lng). A missing file means no restrooms are mapped in that cell.
- `v1/index.json` — cell list with record counts, sizes and hashes.

Records are compact arrays:
`[id, lat, lng, name, kind, fee, key, wheelchair, changing_table, unisex, access, opening_hours]`
(`kind`: `p` public toilet / `c` toilets inside a business; flags `y`/`n`/`l`/`''`).

## License

Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), made available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This repository is a
derived database and is distributed under the same license. Extracted from the OpenStreetMap planet via
[QLever](https://qlever.dev); built by `scripts/restroom-tiles/build_tiles.py` in the Throne Score app repo.
