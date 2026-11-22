# Visualizer screenshots

`visualizer-demo.png` and `visualizer-demo-dark.png` are browser captures of the
real Marvin Pilot visualizer using entirely fictional data. They contain no
Marvin account content or credentials.

`visualizer-demo.json` is a preview-only illustration, **not** a plan to apply
to a Marvin account. To regenerate both images after visualizer changes, install
the development dependencies and Chromium for Playwright, then run
`python tools/capture_visualizer_demo.py` from the repository root.

`existing-subtasks/before.png` and `existing-subtasks/after.png` compare the same
fictional completion and rename on the old and new viewers. Both screenshots
use **After only**. The fixture is `tests/fixtures/existing-subtasks-preview.json`;
the baseline does not include the new read-only checklist metadata.
