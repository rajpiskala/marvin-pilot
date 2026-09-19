"""Keep the README usable both on GitHub and as the PyPI long description."""

import re
from pathlib import Path

from marvin_pilot import __version__


def test_readme_links_and_mascot_work_on_pypi() -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")

    assert (
        'src="https://raw.githubusercontent.com/rajpiskala/marvin-pilot/main/'
        'src/marvin_pilot/visualizer_assets/marvin-pilot.png"'
    ) in readme
    for url in re.findall(r"\]\(([^)]+)\)", readme):
        assert url.startswith(("https://", "#", "mailto:")), url
    for url in re.findall(r'(?:src|href)="([^"]+)"', readme):
        assert url.startswith(("https://", "#")), url
    assert f'pipx install "marvin-pilot=={__version__}"' in readme
