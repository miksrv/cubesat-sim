# Build Photos

Photos of the physical CubeSat build — assembly steps, wiring, enclosure, finished unit. Referenced from the main [README.md](../../README.md#build-photos).

## Conventions

- Format: JPEG, resized so the longest edge is ~1400–1600px and re-compressed (quality ~80) before committing — phone camera originals run 3–10 MB each, which is unnecessary for a README. On macOS: `sips -Z 1600 -s formatOptions 82 input.jpeg --out output.jpg`.
- Naming: `NN-short-description.jpg`, e.g. `01-frame-3d-printed.jpg`, `02-hardware-components.jpg`. The leading number controls display order; suffix `-1`/`-2`/`-3` when several shots belong to the same step (e.g. `03-assembled-no-panels-1.jpg`).
- Embed in `README.md` with a relative path:
  ```markdown
  ![3D-printed CubeSat frame](hardware/photos/01-frame-3d-printed.jpg)
  ```
  For multiple photos of the same subject shown side by side, use an HTML `<p align="center">` block with `<img width="32%">` per image (see the "Build Photos" section in the root README for the current example).
