"""Sanity checks on the journey files shipped in journeys/."""

from pathlib import Path

import pytest
import yaml

JOURNEY_FILES = sorted(Path("journeys").glob("*.yaml"))


def test_journeys_exist():
    assert len(JOURNEY_FILES) >= 4


@pytest.mark.parametrize("path", JOURNEY_FILES, ids=lambda p: p.stem)
def test_journey_is_well_formed(path):
    data = yaml.safe_load(path.read_text())
    assert data["name"] == path.stem
    assert data["steps"], "journey needs at least one step"
    for step in data["steps"]:
        assert {"name", "method", "path"} <= set(step)
        assert step["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}
