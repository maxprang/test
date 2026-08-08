"""Thin wrapper around the docker CLI.

Verifiers that need a real service (Postgres, a web app) spin up a throwaway
container against the restored data. We shell out to `docker` rather than depend
on the SDK: every homelab already has the CLI, and the surface we need is tiny.
"""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .util import CommandError, Logger, require_binary, run


class DockerError(RuntimeError):
    """Docker is unavailable or a container misbehaved."""


class Docker:
    def __init__(self, binary: str = "docker", logger: Logger | None = None):
        self.binary = binary
        self.log = logger or Logger(quiet=True)

    def available(self) -> bool:
        try:
            require_binary(self.binary)
        except CommandError:
            return False
        return run([self.binary, "info", "--format", "{{.ServerVersion}}"], timeout=30).ok

    def require(self) -> None:
        if not self.available():
            raise DockerError(
                "docker is not usable (binary missing or daemon unreachable); "
                "this verifier needs a container runtime"
            )

    def run_detached(
        self,
        image: str,
        *,
        name: str | None = None,
        env: dict[str, str] | None = None,
        mounts: list[tuple[Path, str, bool]] | None = None,
        publish: list[int] | None = None,
        command: list[str] | None = None,
        network: str | None = None,
        pull: bool = False,
        extra_args: list[str] | None = None,
        timeout: float = 300,
    ) -> str:
        container = name or f"restore-guard-{secrets.token_hex(4)}"
        argv = [self.binary, "run", "--detach", "--name", container]
        for key, value in (env or {}).items():
            argv += ["--env", f"{key}={value}"]
        for host_path, container_path, read_only in mounts or []:
            suffix = ":ro" if read_only else ""
            argv += ["--volume", f"{Path(host_path).resolve()}:{container_path}{suffix}"]
        for port in publish or []:
            argv += ["--publish", f"127.0.0.1::{port}"]
        if network:
            argv += ["--network", network]
        argv += extra_args or []
        argv.append(image)
        argv += command or []

        if pull:
            run([self.binary, "pull", image], timeout=timeout)

        result = run(argv, timeout=timeout)
        if not result.ok:
            raise DockerError(f"docker run {image} failed: {result.tail()}")
        return container

    def exec(
        self,
        container: str,
        command: list[str],
        *,
        timeout: float = 120,
        user: str | None = None,
        stdin_text: str | None = None,
    ):
        argv = [self.binary, "exec"]
        if stdin_text is not None:
            argv.append("--interactive")
        if user:
            argv += ["--user", user]
        argv.append(container)
        argv += command
        return run(argv, timeout=timeout, stdin_text=stdin_text)

    def port(self, container: str, container_port: int) -> int:
        result = run([self.binary, "port", container, str(container_port)], timeout=30)
        if not result.ok or not result.stdout.strip():
            raise DockerError(f"container {container} does not publish port {container_port}")
        # Output looks like "127.0.0.1:49154" (possibly several lines).
        first = result.stdout.strip().splitlines()[0]
        try:
            return int(first.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise DockerError(f"cannot parse docker port output {first!r}") from exc

    def logs(self, container: str, tail: int = 40) -> str:
        result = run([self.binary, "logs", "--tail", str(tail), container], timeout=60)
        return (result.stdout + result.stderr).strip()

    def copy_in(self, container: str, source: Path, dest: str, timeout: float = 300) -> None:
        result = run([self.binary, "cp", str(source), f"{container}:{dest}"], timeout=timeout)
        if not result.ok:
            raise DockerError(f"docker cp into {container} failed: {result.tail()}")

    def remove(self, container: str) -> None:
        run([self.binary, "rm", "--force", "--volumes", container], timeout=120)

    def wait_healthy(
        self,
        container: str,
        probe: list[str],
        *,
        timeout: float,
        interval: float = 2.0,
    ) -> None:
        """Poll an in-container command until it succeeds or we run out of patience."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            if not self._running(container):
                raise DockerError(
                    f"container {container} exited during startup:\n{self.logs(container)}"
                )
            result = self.exec(container, probe, timeout=min(30, timeout))
            if result.ok:
                return
            last = result.tail(3)
            time.sleep(interval)
        raise DockerError(
            f"container {container} not ready after {timeout:.0f}s "
            f"(last probe: {last or 'no output'})"
        )

    def _running(self, container: str) -> bool:
        result = run(
            [self.binary, "inspect", "--format", "{{.State.Running}}", container], timeout=30
        )
        return result.ok and result.stdout.strip() == "true"

    @contextmanager
    def ephemeral(self, *args, **kwargs) -> Iterator[str]:
        """Run a container and guarantee it is gone afterwards, success or not."""
        container = self.run_detached(*args, **kwargs)
        try:
            yield container
        finally:
            self.remove(container)
