"""Exercise the demo publisher's byte limit without recording or installing."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("size", [9_999_999, 10_000_000, 10_000_001])
@pytest.mark.parametrize("posixly_correct", [False, True], ids=["default", "posix"])
@pytest.mark.parametrize("demo_name", ["demo", "recall"])
def test_gif_byte_limit_preserves_previous_output_on_failure(tmp_path, size, posixly_correct, demo_name):
    assets = tmp_path / "assets"
    assets.mkdir()
    script = assets / "make-demo-gif"
    shutil.copyfile(Path(__file__).parents[1] / "assets" / script.name, script)
    (assets / f"{demo_name}.tape").touch()
    output = assets / f"{demo_name}.gif"
    output.write_bytes(b"previous valid GIF")
    other_output = assets / ("demo.gif" if demo_name == "recall" else "recall.gif")
    other_output.write_bytes(b"separate recording")

    executables = tmp_path / "bin"
    executables.mkdir()
    for name, source in {
        "vhs": "from pathlib import Path\nPath(sys.argv[2]).write_bytes(b'recording')\n",
        "ffmpeg": (
            "import os\n"
            "with open(sys.argv[-1], 'wb') as output:\n"
            "    output.truncate(int(os.environ['DEMO_TEST_BYTES']))\n"
        ),
    }.items():
        executable = executables / name
        executable.write_text(f"#!{sys.executable}\nimport sys\n{source}")
        executable.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{executables}{os.pathsep}{os.environ['PATH']}",
        DEMO_TEST_BYTES=str(size),
    )
    env.pop("POSIXLY_CORRECT", None)
    if posixly_correct:
        # Make GNU utilities stop at the first operand, as BSD utilities do.
        env["POSIXLY_CORRECT"] = "1"
    result = subprocess.run(
        ["bash", str(script), demo_name], env=env, capture_output=True, text=True, check=False,
    )

    if size < 10_000_000:
        assert result.returncode == 0, result.stderr
        assert output.stat().st_size == size
        assert output.stat().st_mode & 0o777 == 0o644
    else:
        assert result.returncode == 1
        assert "must be below 10000000 bytes" in result.stderr
        assert output.read_bytes() == b"previous valid GIF"
    assert not list(assets.glob(".make-demo-gif.*"))
    assert other_output.read_bytes() == b"separate recording"
