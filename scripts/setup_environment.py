#!/usr/bin/env python3
"""Install project dependencies, deploy local services, and report readiness.

The script intentionally depends on the Python standard library only so it can
run before ``uv sync`` has created the project environment.
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10's system interpreter can bootstrap uv.
    tomllib = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "environment.toml"
VALID_COMPONENTS = {"clickhouse", "searxng", "lightrag"}


def run(command: list[str], *, env: dict[str, str]) -> None:
    print("\n$ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=env)
    if completed.returncode:
        raise RuntimeError(f"command failed with exit status {completed.returncode}")


def read_config(path: Path) -> dict:
    try:
        if tomllib is not None:
            with path.open("rb") as handle:
                config = tomllib.load(handle)
        else:
            config = parse_simple_toml(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"configuration file not found: {path}") from exc
    except (ValueError, SyntaxError) as exc:
        raise RuntimeError(f"invalid TOML in {path}: {exc}") from exc

    components = config.get("services", {}).get("components", [])
    unknown = set(components) - VALID_COMPONENTS
    if unknown:
        raise RuntimeError(f"unknown services.components values: {', '.join(sorted(unknown))}")
    if not components:
        raise RuntimeError("services.components must include at least one component")
    return config


def parse_simple_toml(content: str) -> dict:
    """Parse the scalar/list TOML subset used by environment.toml on Python 3.10."""
    result: dict = {}
    section = result
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            if not section_name or "." in section_name:
                raise ValueError(f"unsupported TOML section: {raw_line}")
            section = result.setdefault(section_name, {})
            continue
        if "=" not in line:
            raise ValueError(f"expected key = value: {raw_line}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"empty key: {raw_line}")
        if value == "true":
            parsed = True
        elif value == "false":
            parsed = False
        elif value.startswith("[") or value.startswith('"') or value.startswith("'"):
            parsed = ast.literal_eval(value)
        else:
            try:
                parsed = int(value)
            except ValueError:
                try:
                    parsed = float(value)
                except ValueError:
                    raise ValueError(f"unsupported value: {raw_line}") from None
        section[key] = parsed
    return result


def runtime_environment(config: dict) -> dict[str, str]:
    clickhouse = config.get("clickhouse", {})
    environment = os.environ.copy()
    mappings = {
        "CLICKHOUSE_HOST": clickhouse.get("host", "localhost"),
        "CLICKHOUSE_PORT": clickhouse.get("http_port", 8123),
        "CLICKHOUSE_HTTP_PORT": clickhouse.get("http_port", 8123),
        "CLICKHOUSE_NATIVE_PORT": clickhouse.get("native_port", 9000),
        "CLICKHOUSE_USER": clickhouse.get("user", "default"),
        "CLICKHOUSE_PASSWORD": clickhouse.get("password", ""),
        "CLICKHOUSE_DATABASE": clickhouse.get("database", "quant"),
        "CLICKHOUSE_INSERT_CHUNK_ROWS": clickhouse.get("insert_chunk_rows", 50000),
    }
    environment.update({key: str(value) for key, value in mappings.items()})
    return environment


def ensure_tooling(config: dict, env: dict[str, str]) -> str:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is not installed; install it with: curl -LsSf https://astral.sh/uv/install.sh | sh")

    python_config = config.get("python", {})
    version = str(python_config.get("version", "3.12.3"))
    run([uv, "python", "install", version], env=env)
    if python_config.get("sync_dev_dependencies", True):
        run([uv, "sync", "--dev"], env=env)
    else:
        run([uv, "sync"], env=env)
    return uv


def command_output(command: list[str], *, env: dict[str, str], timeout: int = 30) -> str:
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        raise RuntimeError(f"{' '.join(command)} failed: {detail[:300]}")
    return completed.stdout.strip()


def retry(command: list[str], *, env: dict[str, str], attempts: int) -> None:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            run(command, env=env)
            return
        except (OSError, RuntimeError) as exc:
            last_error = str(exc)
            if attempt < attempts:
                print(f"[RETRY] attempt {attempt}/{attempts}: {last_error}", flush=True)
                time.sleep(2)
    raise RuntimeError(last_error)


def health_url(url: str, timeout: int = 5) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, URLError):
        return False


def docker_names(env: dict[str, str]) -> set[str]:
    try:
        output = command_output(["docker", "ps", "-a", "--format", "{{.Names}}"], env=env)
    except RuntimeError:
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


def compose_base(env: dict[str, str]) -> list[str]:
    try:
        command_output(["docker", "compose", "version"], env=env)
        return ["docker", "compose"]
    except RuntimeError:
        if shutil.which("docker-compose"):
            return ["docker-compose"]
        raise RuntimeError("Docker Compose is not available")


def ensure_server_env() -> None:
    target = PROJECT_ROOT / "deploy" / "lightrag" / "server.env"
    example = PROJECT_ROOT / "deploy" / "lightrag" / "server.env.example"
    if not target.exists():
        if not example.exists():
            raise RuntimeError(f"missing {example}")
        shutil.copy2(example, target)
        print(f"[OK] created {target.relative_to(PROJECT_ROOT)}")


def deploy_clickhouse(config: dict, env: dict[str, str], attempts: int) -> None:
    clickhouse = config.get("clickhouse", {})
    data_dir = PROJECT_ROOT / "assets" / "clickhouse"
    data_dir.mkdir(parents=True, exist_ok=True)
    if "clickhouse" in docker_names(env):
        retry(["docker", "start", "clickhouse"], env=env, attempts=attempts)
    else:
        retry([
            "docker", "run", "-d", "--name", "clickhouse", "--restart", "unless-stopped",
            "-p", f"{clickhouse.get('http_port', 8123)}:8123",
            "-p", f"{clickhouse.get('native_port', 9000)}:9000",
            "-v", f"{data_dir}:/var/lib/clickhouse",
            "-e", "CLICKHOUSE_USER=default",
            "-e", f"CLICKHOUSE_PASSWORD={clickhouse.get('password', '')}",
            "-e", f"CLICKHOUSE_DB={clickhouse.get('database', 'quant')}",
            "clickhouse/clickhouse-server",
        ], env=env, attempts=attempts)
    url = f"http://127.0.0.1:{clickhouse.get('http_port', 8123)}/ping"
    if not health_url(url, timeout=10):
        raise RuntimeError(f"ClickHouse health check failed: {url}")
    print(f"[OK] ClickHouse healthy: {url}")


def deploy_searxng(env: dict[str, str], attempts: int) -> None:
    root = PROJECT_ROOT / "deploy" / "searxng"
    (root / "searxng").mkdir(parents=True, exist_ok=True)
    (root / "cache").mkdir(parents=True, exist_ok=True)
    port = int(env.get("SEARXNG_PORT", "8888"))
    compose = compose_base(env) + ["-f", str(root / "docker-compose.yml"), "up", "-d"]
    try:
        retry(compose, env=env, attempts=attempts)
    except RuntimeError as compose_error:
        print(f"[WARN] SearXNG compose failed, trying Docker fallback: {compose_error}")
        if "quant-searxng" in docker_names(env):
            retry(["docker", "start", "quant-searxng"], env=env, attempts=attempts)
        else:
            retry([
                "docker", "run", "-d", "--name", "quant-searxng", "--restart", "unless-stopped",
                "--add-host", "host.docker.internal:host-gateway", "-p", f"127.0.0.1:{port}:8080",
                "-v", f"{root / 'searxng'}:/etc/searxng:rw", "-v", f"{root / 'cache'}:/var/cache/searxng:rw",
                "-e", "SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml", "docker.io/searxng/searxng:latest",
            ], env=env, attempts=attempts)
    url = f"http://127.0.0.1:{port}/search?q=quant&format=json&language=zh-CN&categories=general"
    if not health_url(url, timeout=15):
        raise RuntimeError(f"SearXNG health check failed: http://127.0.0.1:{port}")
    print(f"[OK] SearXNG healthy: http://127.0.0.1:{port}")


def deploy_lightrag(config: dict, env: dict[str, str], attempts: int) -> None:
    ensure_server_env()
    compose = compose_base(env)
    services = config.get("services", {})
    if services.get("with_ollama", False):
        compose += ["--profile", "ollama"]
    compose += [
        "--env-file", str(PROJECT_ROOT / "deploy" / "lightrag" / "server.env"),
        "-f", str(PROJECT_ROOT / "deploy" / "lightrag" / "docker-compose.yml"), "up", "-d",
    ]
    retry(compose, env=env, attempts=attempts)
    for _ in range(30):
        try:
            command_output(["docker", "exec", "stock-lightrag-postgres", "pg_isready"], env=env)
            print("[OK] LightRAG PostgreSQL healthy")
            break
        except RuntimeError:
            time.sleep(1)
    else:
        raise RuntimeError("LightRAG PostgreSQL health check timed out")
    port = int(env.get("OLLAMA_PORT", "11434"))
    if services.get("with_ollama", False):
        if not health_url(f"http://127.0.0.1:{port}/api/tags", timeout=10):
            raise RuntimeError("Ollama health check failed")
        if not services.get("skip_ollama_pull", False):
            retry(["docker", "exec", "stock-lightrag-ollama", "ollama", "pull", "bge-m3"], env=env, attempts=attempts)
        print(f"[OK] Ollama healthy: http://127.0.0.1:{port}")
    else:
        print("[SKIP] Ollama profile disabled in config")


def deploy_services(config: dict, env: dict[str, str]) -> None:
    services = config.get("services", {})
    if not services.get("auto_deploy", True):
        print("\n[SKIP] services.auto_deploy=false; deployment skipped")
        return
    if not shutil.which("docker"):
        raise RuntimeError("docker is not installed")
    try:
        command_output(["docker", "info"], env=env)
    except RuntimeError as exc:
        raise RuntimeError(f"Docker daemon is not reachable: {exc}") from exc
    attempts = max(1, int(services.get("retries", 2)))
    failures = []
    for component in services["components"]:
        try:
            if component == "clickhouse":
                deploy_clickhouse(config, env, attempts)
            elif component == "searxng":
                deploy_searxng(env, attempts)
            elif component == "lightrag":
                deploy_lightrag(config, env, attempts)
        except (OSError, RuntimeError) as exc:
            failures.append(f"{component}: {exc}")
            print(f"[FAIL] {component}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError("; ".join(failures))


def run_preflight(config: dict, env: dict[str, str], uv: str) -> None:
    preflight = config.get("preflight", {})
    command = [
        uv,
        "run",
        "python",
        "scripts/check_cn_pipeline.py",
        "--min-free-gb",
        str(preflight.get("min_free_gb", 20)),
        "--min-ohlcv-rows",
        str(preflight.get("min_ohlcv_rows", 120)),
    ]
    if not preflight.get("online_sources", True):
        command.append("--skip-online")
    run(command, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the local environment and run the CN pipeline preflight.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML configuration file")
    parser.add_argument("--check-only", action="store_true", help="Skip deployment and only run environment checks")
    args = parser.parse_args()

    config_path = args.config if args.config.is_absolute() else (PROJECT_ROOT / args.config)
    try:
        config = read_config(config_path)
        env = runtime_environment(config)
        print(f"Configuration: {config_path}")
        uv = ensure_tooling(config, env)
        if not args.check_only:
            deploy_services(config, env)
        run_preflight(config, env, uv)
        print("\nEnvironment setup completed.")
        return 0
    except RuntimeError as exc:
        print(f"\nEnvironment setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
