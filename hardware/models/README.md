# 3D Models

CAD/3D-printable files used to build the physical CubeSat frame and mounts (e.g. `.stl`, `.step`/`.stp`, source CAD files such as `.f3d`/`.FCStd`). Referenced from the main [README.md](../../README.md#3d-models).

## Conventions

- Keep one subfolder per printable/machinable part or assembly, e.g. `frame/`, `base/`.
- Include both the source CAD file (for editing) and an exported `.stl`/`.step` (for printing/reference) where practical.
- Large binary CAD files bloat git history — if a source file is regularly re-exported and gets large, consider Git LFS instead of committing every revision directly.

## Current files

| File | Purpose |
|---|---|
| `base/CS_MGSE_Tight_Fit.stl` | Ground support stand — holds the assembled CubeSat upright on a bench |
| `frame/Cubesat_Bottom_Frame.stl` | Bottom frame panel |
| `frame/Cubesat_Side_Frame_Plain.stl` | Side frame panel |
| `frame/Cubesat_Adaptor_Mount.stl` | Internal adaptor mount for the electronics stack |
| `frame/Cubesat_RaspbiCam_Frame.stl` | Camera mount for the Raspberry Pi Camera Module |
