# chromaprint.py (vendored)

Low-level ctypes wrapper around `libchromaprint`, used by `fingerprint_kit.py`
to decode a fingerprint back into bits for comparison.

Vendored, not pip-installed, on purpose: PyPI's `chromaprint` package is an
unrelated project (terminal color output) that happens to squat the name —
`pip install chromaprint` installs the WRONG package silently. This exact
file has only ever been distributed as a source file inside the pyacoustid
project's own repo, never as its own PyPI distribution.

Source: https://github.com/beetbox/pyacoustid/blob/master/chromaprint.py
(unmodified copy, MIT licensed, see LICENSE)
