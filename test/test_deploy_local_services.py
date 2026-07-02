import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_local_services.sh"


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=command_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_deploy_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_deploy_script_help_documents_components() -> None:
    result = run_script("--help")

    assert result.returncode == 0, result.stderr
    assert "clickhouse" in result.stdout
    assert "searxng" in result.stdout
    assert "lightrag" in result.stdout
    assert "--components" in result.stdout
    assert "--with-ollama" in result.stdout


def test_lightrag_postgres_uses_local_build_instead_of_blocked_custom_image() -> None:
    compose = (ROOT / "deploy" / "lightrag" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert "gzdaniel/postgres-for-rag" not in compose
    assert "Dockerfile.postgres" in compose
    assert "pull_policy: ${LIGHTRAG_POSTGRES_PULL_POLICY:-build}" in compose
    assert "${LIGHTRAG_POSTGRES_BUILD_CONTEXT:-../../../LightRAG}" in compose


def test_lightrag_ollama_is_opt_in_profile_to_avoid_large_default_pull() -> None:
    compose = (ROOT / "deploy" / "lightrag" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert "profiles:" in compose
    assert '"ollama"' in compose
    assert "${OLLAMA_IMAGE:-docker.m.daocloud.io/ollama/ollama:latest}" in compose


def test_lightrag_up_creates_server_env_without_overwriting_existing(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_calls = tmp_path / "docker.calls"
    curl_calls = tmp_path / "curl.calls"

    docker = fake_bin / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {docker_calls}
case "$1" in
  info) exit 0 ;;
  compose) exit 0 ;;
  exec) exit 0 ;;
  ps) exit 1 ;;
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    curl = fake_bin / "curl"
    curl.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {curl_calls}
printf '{{}}'
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    env_path = ROOT / "deploy" / "lightrag" / "server.env"
    backup_path = tmp_path / "server.env.backup"
    had_existing = env_path.exists()
    if had_existing:
        shutil.copy2(env_path, backup_path)
        env_path.unlink()

    try:
        result = run_script(
            "up",
            "--components",
            "lightrag",
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert env_path.exists()
        assert "created deploy/lightrag/server.env from example" in result.stdout

        custom_text = "POSTGRES_PASSWORD=do_not_overwrite\n"
        env_path.write_text(custom_text, encoding="utf-8")

        second = run_script(
            "up",
            "--components",
            "lightrag",
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )

        assert second.returncode == 0, second.stderr + second.stdout
        assert env_path.read_text(encoding="utf-8") == custom_text
    finally:
        if had_existing:
            shutil.copy2(backup_path, env_path)
        else:
            env_path.unlink(missing_ok=True)
