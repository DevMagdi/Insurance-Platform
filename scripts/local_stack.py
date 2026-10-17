"""Run the platform locally without a Docker daemon.

``make up`` expects a container runtime. Sandboxes, CI images and locked-down
desktops often do not have one, which leaves the SPA unreachable and the
preview behind a "Bad Gateway". This script starts the same dependencies as
``docker-compose.yml`` from the pinned wheels in the backend's ``localstack``
extra — ``pgserver`` ships a PostgreSQL server, ``redislite`` ships a Redis
server — and then runs the API, the worker and the Vite dev server directly.

    pip install -e './backend[localstack]'
    make init            # only once: creates the git-ignored .env
    make local-up        # postgres + redis + smtp + api + worker + vite
    make local-down

The services keep the same ports and credentials as the Compose stack, so the
browser talks to ``http://localhost:5173/`` and the API to
``http://127.0.0.1:8000/``. State lives in ``.local-stack/`` (git-ignored).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local-stack"
LOGS = STATE / "logs"
MAILDIR = STATE / "mail"
PGDATA = STATE / "pgdata"
PGSOCK = STATE / "pgsock"
REDISDIR = STATE / "redis"

POSTGRES_PORT = 5432
REDIS_PORT = 6379
SMTP_PORT = 1025
API_PORT = 8000
VITE_PORT = 5173

SERVICES = ("postgres", "redis", "smtp", "api", "worker", "vite")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    print(f"[local-stack] {message}", flush=True)


def read_env() -> dict[str, str]:
    """Parse the git-ignored .env that `make init` generated."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        raise SystemExit(
            "Missing .env - run `make init` first (it creates local-only secrets)."
        )
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def wait_for_port(port: int, name: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            log(f"{name} is accepting connections on 127.0.0.1:{port}")
            return
        time.sleep(0.4)
    raise SystemExit(
        f"{name} did not start listening on 127.0.0.1:{port} within {timeout:.0f}s"
    )


def pid_file(name: str) -> Path:
    return STATE / f"{name}.pid"


def read_pid(name: str) -> int | None:
    path = pid_file(name)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def spawn(name: str, command: list[str], env: dict[str, str], cwd: Path) -> int:
    """Start a background service, log its output and record its PID."""
    if read_pid(name) is not None:
        log(f"{name} is already running")
        return 0
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = (LOGS / f"{name}.log").open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=logfile,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file(name).write_text(f"{process.pid}\n", encoding="utf-8")
    log(f"{name} started (pid {process.pid}); logs in .local-stack/logs/{name}.log")
    return process.pid


def stop(name: str) -> None:
    pid = read_pid(name)
    if pid is None:
        log(f"{name} is not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    pid_file(name).unlink(missing_ok=True)
    log(f"{name} stopped")


# --------------------------------------------------------------------------- #
# service binaries shipped by the localstack extra
# --------------------------------------------------------------------------- #
def pg_bin() -> Path:
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover - developer convenience
        raise SystemExit(
            "pgserver is missing - run `pip install -e './backend[localstack]'`."
        ) from exc
    binaries = Path(pgserver.__file__).parent / "pginstall" / "bin"
    if not binaries.is_dir():
        raise SystemExit(f"pgserver binaries are missing at {binaries}")
    return binaries


def redis_bin() -> Path:
    try:
        import redislite
    except ImportError as exc:  # pragma: no cover - developer convenience
        raise SystemExit(
            "redislite is missing - run `pip install -e './backend[localstack]'`."
        ) from exc
    binary = Path(redislite.__file__).parent / "bin" / "redis-server"
    if not binary.is_file():
        raise SystemExit(f"redislite binary is missing at {binary}")
    return binary


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #
def start_postgres(env: dict[str, str]) -> None:
    binaries = pg_bin()
    PGSOCK.mkdir(parents=True, exist_ok=True)
    if not (PGDATA / "PG_VERSION").exists():
        shutil.rmtree(PGDATA, ignore_errors=True)
        log("initialising the PostgreSQL cluster")
        subprocess.run(
            [
                str(binaries / "initdb"),
                "--auth=trust",
                "--auth-local=trust",
                "--encoding=utf8",
                "-U",
                env["POSTGRES_USER"],
                "-D",
                str(PGDATA),
            ],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        [
            str(binaries / "pg_ctl"),
            "-w",
            "-D",
            str(PGDATA),
            "-o",
            f"-h 127.0.0.1 -p {POSTGRES_PORT} -k {PGSOCK} -c listen_addresses=127.0.0.1",
            "-l",
            str(STATE / "postgres.log"),
            "start",
        ],
        check=True,
        capture_output=True,
    )
    wait_for_port(POSTGRES_PORT, "postgres")


def create_database(env: dict[str, str]) -> None:
    """Create the dev database and the restricted roles (see infra/postgres/init)."""
    import psycopg
    from psycopg import sql

    admin = (
        f"host=127.0.0.1 port={POSTGRES_PORT} user={env['POSTGRES_USER']} "
        f"password={env['POSTGRES_PASSWORD']} dbname=postgres"
    )
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (env["POSTGRES_DB"],)
        )
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{env["POSTGRES_DB"]}"')
            log(f"created database {env['POSTGRES_DB']}")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime'")
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL("CREATE ROLE app_runtime LOGIN NOBYPASSRLS PASSWORD {}").format(
                    sql.Literal(env["APP_DB_PASSWORD"])
                )
            )
            log("created role app_runtime")
        cur.execute(
            "ALTER ROLE app_runtime NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB"
        )
        cur.execute(f'GRANT CONNECT ON DATABASE "{env["POSTGRES_DB"]}" TO app_runtime')
        # The worker login is provisioned next to app_runtime so its password
        # never depends on the environment of the migrating process.
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'app_worker'")
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL("CREATE ROLE app_worker LOGIN PASSWORD {}").format(
                    sql.Literal(env["APP_WORKER_DB_PASSWORD"])
                )
            )
            log("created role app_worker")


def run_migrations(env: dict[str, str]) -> None:
    admin_url = (
        f"postgresql+psycopg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{POSTGRES_PORT}/{env['POSTGRES_DB']}"
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT / "backend"),
        env={**os.environ, **env, "DATABASE_ADMIN_URL": admin_url},
        check=True,
    )


def ensure_worker_role(env: dict[str, str]) -> None:
    """Make sure the restricted worker role can log in.

    A fresh cluster gets ``app_worker`` from ``create_database`` and the init
    script in the Compose stack, but revision 006 creates the role as NOLOGIN
    and only promotes it when ``APP_WORKER_DB_PASSWORD`` is present in the
    migrating process. A cluster that was migrated without that variable keeps
    the role unusable, and Alembic will not replay the revision, so the
    promotion is repeated here.
    """
    import psycopg
    from psycopg import sql

    admin = (
        f"host=127.0.0.1 port={POSTGRES_PORT} user={env['POSTGRES_USER']} "
        f"password={env['POSTGRES_PASSWORD']} dbname=postgres"
    )
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'app_worker'")
        if cur.fetchone() is None:
            cur.execute("CREATE ROLE app_worker NOLOGIN")
            log("created role app_worker")
        cur.execute(
            sql.SQL("ALTER ROLE app_worker LOGIN PASSWORD {}").format(
                sql.Literal(env["APP_WORKER_DB_PASSWORD"])
            )
        )
        cur.execute(
            "ALTER ROLE app_worker NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS"
        )
        cur.execute(f'GRANT CONNECT ON DATABASE "{env["POSTGRES_DB"]}" TO app_worker')


def start_redis(env: dict[str, str]) -> None:
    REDISDIR.mkdir(parents=True, exist_ok=True)
    spawn(
        "redis",
        [
            str(redis_bin()),
            "--port",
            str(REDIS_PORT),
            "--bind",
            "127.0.0.1",
            "--dir",
            str(REDISDIR),
            "--dbfilename",
            "dump.rdb",
            "--save",
            "",
        ],
        env={**os.environ},
        cwd=ROOT,
    )
    wait_for_port(REDIS_PORT, "redis")


def start_smtp(env: dict[str, str]) -> None:
    MAILDIR.mkdir(parents=True, exist_ok=True)
    spawn(
        "smtp",
        [
            sys.executable,
            str(ROOT / "scripts" / "local_smtp.py"),
            "--port",
            str(SMTP_PORT),
            "--maildir",
            str(MAILDIR),
        ],
        env={**os.environ},
        cwd=ROOT,
    )
    wait_for_port(SMTP_PORT, "smtp")


def service_env(env: dict[str, str]) -> dict[str, str]:
    """Shared runtime environment for the API and the worker."""
    return {
        **os.environ,
        **env,
        "ENVIRONMENT": "development",
        "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(SMTP_PORT),
        "DEV_SMS_MAILBOX": "true",
        "LOG_JSON": "false",
    }


def start_api(env: dict[str, str]) -> None:
    runtime = service_env(env)
    runtime["DATABASE_URL"] = (
        f"postgresql+asyncpg://app_runtime:{env['APP_DB_PASSWORD']}"
        f"@127.0.0.1:{POSTGRES_PORT}/{env['POSTGRES_DB']}"
    )
    # The Compose stack publishes the app through Nginx on 8080; without Docker
    # the Vite dev server is the origin, unless the developer chose a tunnel URL.
    public_url = env.get("PUBLIC_APP_URL", "")
    if not public_url or public_url == "http://localhost:8080":
        public_url = f"http://localhost:{VITE_PORT}"
    runtime["PUBLIC_APP_URL"] = public_url
    spawn(
        "api",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(API_PORT),
        ],
        env=runtime,
        cwd=ROOT / "backend",
    )
    wait_for_port(API_PORT, "api")


def start_worker(env: dict[str, str]) -> None:
    runtime = service_env(env)
    runtime["WORKER_DATABASE_URL"] = (
        f"postgresql+asyncpg://app_worker:{env['APP_WORKER_DB_PASSWORD']}"
        f"@127.0.0.1:{POSTGRES_PORT}/{env['POSTGRES_DB']}"
    )
    spawn(
        "worker",
        [sys.executable, "-m", "arq", "app.worker.WorkerSettings"],
        env=runtime,
        cwd=ROOT / "backend",
    )


def start_vite(env: dict[str, str]) -> None:
    if not (ROOT / "frontend" / "node_modules").is_dir():
        raise SystemExit(
            "frontend dependencies are missing - run `npm ci` in frontend/."
        )
    spawn(
        "vite",
        ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", str(VITE_PORT)],
        env={**os.environ, "API_PROXY_TARGET": f"http://127.0.0.1:{API_PORT}"},
        cwd=ROOT / "frontend",
    )
    wait_for_port(VITE_PORT, "vite")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def command_up() -> int:
    env = read_env()
    STATE.mkdir(parents=True, exist_ok=True)
    start_postgres(env)
    create_database(env)
    run_migrations(env)
    ensure_worker_role(env)
    start_redis(env)
    start_smtp(env)
    start_api(env)
    start_worker(env)
    start_vite(env)
    log("")
    log(f"app      -> http://localhost:{VITE_PORT}/")
    log(f"api      -> http://127.0.0.1:{API_PORT}/api/health/ready")
    log(f"mailbox  -> {MAILDIR}")
    log("run `make local-bootstrap-admin` for the first admin (no demo data)")
    log("or `make local-seed` to load the demo dataset")
    return 0


def command_down() -> int:
    for name in ("vite", "worker", "api", "smtp", "redis"):
        stop(name)
    binaries = pg_bin()
    if (PGDATA / "PG_VERSION").exists():
        subprocess.run(
            [str(binaries / "pg_ctl"), "-D", str(PGDATA), "-m", "fast", "stop"],
            check=False,
            capture_output=True,
        )
        log("postgres stopped")
    return 0


def command_status() -> int:
    for name in SERVICES:
        if name == "postgres":
            running = (PGDATA / "postmaster.pid").exists() and port_open(POSTGRES_PORT)
        else:
            running = read_pid(name) is not None
        print(f"{name:9s} {'running' if running else 'stopped'}")
    return 0


def command_bootstrap_admin() -> int:
    """Create the first administrator only; the command prompts on stdin."""
    env = read_env()
    if not port_open(POSTGRES_PORT):
        raise SystemExit("PostgreSQL is not running - start it with `make local-up`.")
    run_migrations(env)
    ensure_worker_role(env)
    database_admin_url = (
        f"postgresql+psycopg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{POSTGRES_PORT}/{env['POSTGRES_DB']}"
    )
    subprocess.run(
        [sys.executable, "-m", "app.scripts.bootstrap_admin"],
        cwd=str(ROOT / "backend"),
        env={**os.environ, **env, "DATABASE_ADMIN_URL": database_admin_url},
        check=True,
    )
    return 0


def command_seed() -> int:
    env = read_env()
    if not port_open(POSTGRES_PORT):
        raise SystemExit("PostgreSQL is not running - start it with `make local-up`.")
    run_migrations(env)
    ensure_worker_role(env)
    if not env.get("DEMO_STAFF_PASSWORD"):
        raise SystemExit(
            "Set DEMO_STAFF_PASSWORD (12+ characters) in .env before seeding demo data."
        )
    admin_url = (
        f"postgresql+psycopg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{POSTGRES_PORT}/{env['POSTGRES_DB']}"
    )
    subprocess.run(
        [sys.executable, "-m", "app.scripts.seed"],
        cwd=str(ROOT / "backend"),
        env={**os.environ, **env, "DATABASE_ADMIN_URL": admin_url},
        check=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("up", "down", "status", "bootstrap-admin", "seed")
    )
    args = parser.parse_args()
    handlers = {
        "up": command_up,
        "down": command_down,
        "status": command_status,
        "bootstrap-admin": command_bootstrap_admin,
        "seed": command_seed,
    }
    return handlers[args.command]()


if __name__ == "__main__":
    sys.exit(main())
