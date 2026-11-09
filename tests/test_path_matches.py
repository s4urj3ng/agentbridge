from agentbridge.policies.safety import path_matches

F = "chdk-port/platform/g3x/sub/110c/osd2_test_overlay.h"


def test_trailing_slash_dir_covers_nested_files():
    assert path_matches(F, ["chdk-port/platform/g3x/"])
    assert path_matches(F, ["chdk-port/platform/g3x/sub/110c/"])
    assert path_matches("docs/HANDOFF.md", ["docs/"])
    assert path_matches("tools/build_candidate_port25_ladder_fix.py", ["tools/"])


def test_trailing_slash_dir_does_not_overmatch():
    assert not path_matches("chdk-port/platform/g3xx/lib.c", ["chdk-port/platform/g3x/"])
    assert not path_matches("README.md", ["docs/"])
    assert not path_matches("anything/at/all.c", ["/"])


def test_existing_forms_unchanged():
    assert path_matches(F, ["chdk-port/platform/g3x/**"])
    assert path_matches("docs/PORT36.md", ["docs/PORT36.md"])
    assert path_matches("tests/test_a.py", ["tests/*.py"])
    assert not path_matches("docs/PORT36.md", ["docs/PORT37.md"])
