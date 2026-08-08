"""Start the real application against the restored data and talk HTTP to it.

The strongest check available: mount the restored volume into the service's own
image, wait for it to come up, and fetch a page. If Vaultwarden serves its login
screen off the restored data directory, the backup is good.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

from ..config import ConfigError
from ..docker import Docker, DockerError
from . import VerifyContext, VerifyError, VerifyResult, Verifier, register


@register
class HttpServiceVerifier(Verifier):
    type = "http"

    def validate(self) -> None:
        self._required("image")
        self._required("mount")
        port = self.spec.get("port", 80)
        try:
            int(port)
        except (TypeError, ValueError):
            raise ConfigError(
                f"job {self.job.name!r}: verify.http.port must be a number, got {port!r}"
            ) from None

    def run(self, ctx: VerifyContext) -> VerifyResult:
        started = time.monotonic()
        docker = Docker(str(self.spec.get("docker_binary", "docker")), ctx.log)
        try:
            docker.require()
        except DockerError as exc:
            raise VerifyError(str(exc)) from exc

        subdir = self.spec.get("subdir")
        host_dir = ctx.resolve(str(subdir)) if subdir else ctx.restore_dir
        if not host_dir.exists():
            return self.failed(f"restore does not contain {subdir!r} to mount")

        container_port = int(self.spec.get("port", 80))
        path = str(self.spec.get("path", "/"))
        expect_status = int(self.spec.get("expect_status", 200))
        ready_timeout = min(float(self.spec.get("ready_timeout", 120)), ctx.timeout)

        details: dict[str, Any] = {"image": self.spec["image"], "path": path}
        problems: list[str] = []

        try:
            with docker.ephemeral(
                str(self.spec["image"]),
                env={k: str(v) for k, v in (self.spec.get("env") or {}).items()},
                mounts=[(host_dir, str(self.spec["mount"]), bool(self.spec.get("read_only", True)))],
                publish=[container_port],
                command=list(self.spec.get("command") or []) or None,
                pull=bool(self.spec.get("pull", False)),
                timeout=min(600, ctx.timeout),
            ) as container:
                host_port = docker.port(container, container_port)
                url = f"http://127.0.0.1:{host_port}{path}"
                details["url"] = url

                status, body, error = _poll(url, ready_timeout, self.spec)
                details["status"] = status
                details["ready_seconds"] = round(time.monotonic() - started, 1)

                if status is None:
                    logs = docker.logs(container, 25)
                    problems.append(f"no HTTP response within {ready_timeout:.0f}s ({error})")
                    details["container_logs"] = logs
                else:
                    if status != expect_status:
                        problems.append(f"HTTP {status}, expected {expect_status}")
                        details["container_logs"] = docker.logs(container, 25)
                    for needle in _as_list(self.spec.get("contains")):
                        if str(needle) not in body:
                            problems.append(f"response does not contain {needle!r}")
                    for needle in _as_list(self.spec.get("excludes")):
                        if str(needle) in body:
                            problems.append(f"response unexpectedly contains {needle!r}")
        except DockerError as exc:
            raise VerifyError(f"http verification could not run: {exc}") from exc

        label = str(self.spec["image"]).split("/")[-1]
        summary = f"{label}{path}: " + ("; ".join(problems) if problems else "responded as expected")
        result = self.failed(summary, **details) if problems else self.ok(summary, **details)
        result.duration = time.monotonic() - started
        return result


def _poll(url: str, timeout: float, spec: dict[str, Any]) -> tuple[int | None, str, str]:
    """Poll until the service answers at all; return (status, body, last_error)."""
    deadline = time.monotonic() + timeout
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    last_error = "no attempt made"

    while time.monotonic() < deadline:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(262144).decode("utf-8", "replace")
                return response.status, body, ""
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a real answer — the service is up, just unhappy.
            body = exc.read(262144).decode("utf-8", "replace") if exc.fp else ""
            return exc.code, body, ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = str(getattr(exc, "reason", exc))
            time.sleep(2.0)
    return None, "", last_error


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]
