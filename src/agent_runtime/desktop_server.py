from __future__ import annotations

from pathlib import Path

import click

from agent_runtime.server import run_server


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
    help="正式桌面版内部后端使用的 .env 文件路径。",
)
@click.option("--host", required=True, help="正式桌面版内部后端监听地址。")
@click.option("--port", required=True, type=int, help="正式桌面版内部后端监听端口。")
def main(
    config_file_path: Path,
    env_file_path: Path | None,
    host: str,
    port: int,
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
