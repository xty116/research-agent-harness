#!/usr/bin/env python3
"""Manage only this project's macOS LaunchAgent; never embeds credentials."""
import argparse
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / "Library/Application Support/ResearchAgentHarness"
LABEL = "com.xty.research-agent-harness"
DOMAIN = f"gui/{os.getuid()}"
TARGET = f"{DOMAIN}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def specification():
    return {
        "Label": LABEL,
        "ProgramArguments": [str(RUNTIME / ".venv/bin/python"), "-m", "research_agent.cli",
                             "--config", str(RUNTIME / "config/settings.yaml"), "serve"],
        "WorkingDirectory": str(RUNTIME),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1",
            "PLAYWRIGHT_BROWSERS_PATH": str(RUNTIME / "data/browsers"), "AGENT_ROOT": str(RUNTIME),
            "HF_HOME": str(RUNTIME / "data/hf"), "HF_HUB_DISABLE_XET": "1",
        },
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "ProcessType": "Background", "ExitTimeOut": 20, "Umask": 0o077,
        "StandardOutPath": str(RUNTIME / "data/logs/launchd.stdout.log"),
        "StandardErrorPath": str(RUNTIME / "data/logs/launchd.stderr.log"),
    }


def prepare_runtime():
    import yaml
    from dotenv import dotenv_values, set_key

    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    for name in ("src", "skills", "evals", "config"):
        shutil.copytree(ROOT / name, RUNTIME / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.local.yaml"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, RUNTIME / name)
    source_config = ROOT / "config/settings.local.yaml"
    if not source_config.exists():
        source_config.write_bytes((ROOT / "config/settings.yaml").read_bytes())
    cfg = yaml.safe_load(source_config.read_text())
    values = dotenv_values(ROOT / ".env")
    secret_path = os.environ.get("AGENT_SECRETS_FILE") or values.get("AGENT_SECRETS_FILE") or cfg.get("secrets_file")
    secrets = dotenv_values(secret_path) if secret_path else {}
    target_secrets = RUNTIME / "secrets.env"
    target_secrets.touch(mode=0o600, exist_ok=True)
    target_secrets.chmod(0o600)
    for key in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "OPENALEX_API_KEY", "HF_TOKEN"):
        value = os.environ.get(key) or values.get(key) or secrets.get(key)
        if value:
            set_key(str(target_secrets), key, value)
    # First deployment copies caches/reports and takes consistent SQLite snapshots. Updates never replace live DBs.
    data = Path(cfg["data_dir"])
    data = data if data.is_absolute() else ROOT / data
    reports = Path(cfg["reports_dir"])
    reports = reports if reports.is_absolute() else ROOT / reports
    target_data = RUNTIME / "data"
    if data.exists() and data.resolve() != target_data.resolve():
        shutil.copytree(data, target_data, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "logs", "backups"))
        for database in data.glob("*.sqlite3"):
            target = target_data / database.name
            if not target.exists():
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(target) as dest:
                    source.backup(dest)
    (target_data / "logs").mkdir(parents=True, exist_ok=True)
    if reports.exists() and reports.resolve() != (RUNTIME / "reports").resolve():
        shutil.copytree(reports, RUNTIME / "reports", dirs_exist_ok=True)
    for database in target_data.glob("agent-*.sqlite3"):
        with sqlite3.connect(database) as db:
            for run_id, report_path in db.execute("SELECT id,report_path FROM runs WHERE report_path IS NOT NULL").fetchall():
                if report_path and Path(report_path).is_relative_to(ROOT / "reports"):
                    new_path = RUNTIME / "reports" / Path(report_path).relative_to(ROOT / "reports")
                    db.execute("UPDATE runs SET report_path=? WHERE id=?", (str(new_path), run_id))
    cfg.update(data_dir="data", reports_dir="reports", secrets_file=str(target_secrets))
    cfg["memory"]["embedding_cache"] = "data/models"
    (RUNTIME / "config/settings.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    # Point development commands at the same canonical storage and daily budget ledger.
    cfg.update(data_dir=str(target_data), reports_dir=str(RUNTIME / "reports"),
               secrets_file=secret_path)
    cfg["memory"]["embedding_cache"] = str(target_data / "models")
    source_config.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    interpreter = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
    if not Path(interpreter).exists():
        interpreter = sys.executable
    env = {**os.environ, "UV_CACHE_DIR": str(Path(tempfile.gettempdir()) / "research-agent-uv"),
           "UV_PROJECT_ENVIRONMENT": str(RUNTIME / ".venv")}
    subprocess.run([uv, "sync", "--frozen", "--no-dev", "--extra", "browser", "--extra", "embeddings",
                    "--python", interpreter], cwd=RUNTIME, env=env, check=True)


def launchctl(*arguments, check=True):
    return subprocess.run(["/bin/launchctl", *arguments], check=check, capture_output=True, text=True)


def loaded():
    return launchctl("print", TARGET, check=False).returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["render", "install", "status", "restart", "stop", "start", "uninstall"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Use systemd or Docker on non-macOS systems; this script manages macOS launchd only")
    local = ROOT / "deploy" / f"{LABEL}.plist"
    if args.action == "render":
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(plistlib.dumps(specification()))
        print(local)
        return
    if args.action == "install":
        if not (ROOT / ".venv/bin/python").is_file():
            parser.error("Create the project environment first")
        # Never replace a different application that happens to use this label.
        if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get("WorkingDirectory") not in {str(ROOT), str(RUNTIME)}:
            parser.error("Existing label belongs to a different directory")
        if loaded():
            launchctl("bootout", TARGET)
        prepare_runtime()
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(specification()))
        PLIST.chmod(0o600)
        launchctl("bootstrap", DOMAIN, str(PLIST))
    elif args.action in {"stop", "uninstall"}:
        if loaded():
            launchctl("bootout", TARGET)
        if args.action == "uninstall" and PLIST.exists():
            PLIST.unlink()
    elif args.action == "start":
        if not PLIST.exists():
            parser.error("Run install first")
        if not loaded():
            launchctl("bootstrap", DOMAIN, str(PLIST))
    elif args.action == "restart":
        launchctl("kickstart", "-k", TARGET)
    elif args.action == "status":
        result = launchctl("print", TARGET, check=False)
        print(result.stdout or result.stderr)
        return
    if args.action in {"install", "start", "restart"}:
        from research_agent.settings import load_settings
        settings = load_settings()
        url = f"http://{settings.web.host}:{settings.web.port}/health/ready"
        for _ in range(20):
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    print(response.read().decode())
                return
            except OSError:
                time.sleep(.5)
        raise SystemExit("Service did not become ready; inspect data/logs and service.py status")
    print(f"{LABEL}: {args.action} finished")


if __name__ == "__main__":
    main()
