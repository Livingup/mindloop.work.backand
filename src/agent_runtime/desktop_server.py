from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from agent_runtime.server import run_server


def _open_fallback_stream(env_name: str):
    log_path = os.environ.get(env_name)
    if log_path:
        try:
            return open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            pass
    return open(os.devnull, "w", encoding="utf-8")


def _ensure_standard_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _open_fallback_stream("SPREAD_AI_DESKTOP_BACKEND_STDOUT_LOG")
    if sys.stderr is None:
        sys.stderr = _open_fallback_stream("SPREAD_AI_DESKTOP_BACKEND_STDERR_LOG")


_ensure_standard_streams()


@click.command()
@click.option(
    "--config-file",
    "config_file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="正式桌面版内部后端使用的 agent_runtime.yaml 配置文件路径。",
)
@click.option(
    "--env-file",
    "env_file_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="开发/诊断场景显式传入的 env 覆盖文件；正式 release 启动不使用。",
)
@click.option("--host", default=None, help="开发/诊断场景监听地址覆盖；正式 release 启动不传该参数。")
@click.option("--port", default=None, type=int, help="开发/诊断场景监听端口覆盖；正式 release 启动不传该参数。")
def main(
    config_file_path: Path,
    env_file_path: Path | None,
    host: str | None,
    port: int | None,
) -> None:
    run_server(
        config_file_path=config_file_path,
        env_file_path=env_file_path,
        host=host,
        port=port,
        watch=False,
    )


if __name__ == "__main__":
    main()
