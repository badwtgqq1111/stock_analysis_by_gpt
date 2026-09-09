import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup_environment.py"
CONFIG = ROOT / "config" / "environment.toml"


def test_python_deployment_entrypoint_compiles() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SETUP_SCRIPT)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_default_environment_config_deploys_all_services() -> None:
    content = CONFIG.read_text(encoding="utf-8")

    assert 'components = ["clickhouse", "searxng", "lightrag"]' in content
    assert "auto_deploy = true" in content


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
