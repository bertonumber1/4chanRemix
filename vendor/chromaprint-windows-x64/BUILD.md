# chromaprint.dll (Windows x64)

No official standalone `libchromaprint` build exists for Windows — upstream
only publishes `fpcalc`-only zips (which statically link the library), and
winget's `AcoustID.Chromaprint` package is the same fpcalc-only zip. This
copy was built from source specifically to get a loadable shared library
for `fingerprint_kit.py`'s ctypes-based comparison path.

- Source: https://github.com/acoustid/chromaprint, tag `v1.6.1`,
  commit `aed8eba2202dd9d7b3b0a56c77904cc805490d72`
- Toolchain: MSVC 14.44 (Visual Studio 2022 Build Tools), CMake 4.4.3
- Config: core library only, no `fpcalc`/tests (avoids an FFmpeg
  dependency this app doesn't need — fingerprint GENERATION uses whatever
  `fpcalc` binary is already on the machine, see `fingerprint_kit.py`).

```
cmake -S . -B build -G "Visual Studio 17 2022" -A x64 ^
  -DBUILD_TOOLS=OFF -DBUILD_TESTS=OFF -DFFT_LIB=kissfft -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --target chromaprint
```

`chromaprint.dll` ends up at `build\src\Release\chromaprint.dll`.

`fingerprint_kit.py` prepends this directory to `PATH` before importing the
`chromaprint` pip package on Windows — `ctypes.util.find_library` on
Windows only searches `PATH`, not the importing script's own directory, so
just dropping the DLL next to `python.exe` is not enough on its own.

Rebuild only if a `chromaprint` pip package upgrade breaks ABI compatibility
with 1.6.1, or a newer chromaprint fixes something this app depends on.
