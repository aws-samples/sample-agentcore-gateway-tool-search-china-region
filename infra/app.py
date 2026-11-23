"""CDK entry point with explicit local config; never resolve accounts using AWS."""

from pathlib import Path

import aws_cdk as cdk

from infra.assets import (
    ecs_asset_from_context,
    request_asset_from_context,
    shared_asset_from_context,
)
from infra.config import DeploymentConfig
from infra.stack import SemsearchStack, write_configuration


def main() -> None:
    app = cdk.App()
    config = DeploymentConfig.from_file(
        Path(app.node.try_get_context("config") or "infra/config.example.json"),
        stage=app.node.try_get_context("stage"),
        region=app.node.try_get_context("region"),
    )
    request_asset = request_asset_from_context(
        config.request_package, app.node.try_get_context("request_asset")
    )
    ecs_asset = ecs_asset_from_context(
        config.ecs_search is not None, app.node.try_get_context("ecs_asset")
    )
    shared_asset = shared_asset_from_context(
        config.ecs_search is not None, app.node.try_get_context("asset"), ecs_asset
    )
    write_configuration(config, Path(".artifacts/configuration"))
    SemsearchStack(
        app,
        config.prefix,
        config=config,
        asset_path=shared_asset,
        request_asset_path=request_asset,
        ecs_asset_path=ecs_asset,
        env=cdk.Environment(account=config.account, region=config.region),
    )
    app.synth()


if __name__ == "__main__":
    main()
