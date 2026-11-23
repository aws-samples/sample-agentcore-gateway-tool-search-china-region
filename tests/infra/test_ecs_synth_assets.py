"""New service assets cannot silently replace the currently deployed Lambda packages."""

from test_request_deployment import SHARED_MODULES, archive, run_synth


def test_synth_forwards_separate_old_and_new_assets(monkeypatch, tmp_path):
    old = archive(tmp_path / "existing.zip", SHARED_MODULES)
    new = archive(tmp_path / "new.zip", SHARED_MODULES)
    calls = run_synth(
        monkeypatch, tmp_path, flags=("--asset", str(old), "--ecs-asset", str(new)), ecs=True
    )
    args = calls[0][0]
    assert f"asset={old}" in args
    assert f"ecs_asset={new}" in args
