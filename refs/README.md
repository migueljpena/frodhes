# refs: measurement tools and fits

A copy of the working folder `~/Projects/frodhes-refs` without its audio: the tools that measured the reference
recordings (the Wurlitzer 200A and the CP-70), their fits, the parameter sets of the v4.x test page and the page
itself. The recordings stay on the owner's disk only (`frodhes-refs/audio`, never committed); the tools read them
from there, so the paths in them point at that folder.

- `tools/`: measurement and fitting scripts (`fit_reed.py`, `measure_cp_notes.py`, `summarize_cp_notes.py`, ...)
- `fits/`: the fits' outputs (JSON, CSV, logs)
- `play/`: the v4.x listening page and its parameter sets (`play/cp/`: the CP baseline page; its WAVs not included)
- `test/`: a synthetic test signal's numbers
