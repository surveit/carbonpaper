from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_container_installs_codex_with_the_official_standalone_installer() -> None:
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "https://chatgpt.com/codex/install.sh" in dockerfile
    assert "CODEX_NON_INTERACTIVE=1" in dockerfile
    assert "CODEX_INSTALL_DIR=/usr/local/bin" in dockerfile
    assert "RUN codex --version" in dockerfile


def test_fly_mounts_the_codex_home_the_cli_reads() -> None:
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    fly_config = (_REPO_ROOT / "fly.toml").read_text(encoding="utf-8")
    entrypoint = (_REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "CODEX_HOME=/data/codex" in dockerfile
    assert 'CODEX_HOME = "/data/codex"' in fly_config
    assert '"$CODEX_HOME"' in entrypoint
