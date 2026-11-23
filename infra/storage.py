"""Dedicated per-Gateway storage; retention never deletes an active version."""

from __future__ import annotations

from aws_cdk import Aws, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from constructs import Construct


class Storage(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str) -> None:
        super().__init__(scope, construct_id)
        self.bucket = s3.Bucket(
            self,
            "Index",
            bucket_name=f"{prefix}-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # The TLS deny necessarily covers all principals and all objects in this
            # one bucket. It grants no access. Runtime writes explicitly use AES256.
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # No lifecycle expiry: a static time rule could delete active/previous versions.
        self.table = dynamodb.Table(
            self,
            "State",
            table_name=f"{prefix}-state",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            time_to_live_attribute="expires_at",
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # Authorization/deletion state has its own physical IAM and restore boundary.
        # Match the state table's AWS-managed KMS encryption and retention, with no
        # TTL: an expired tombstone must never resurrect a deleted Gateway.
        self.protected_table = dynamodb.Table(
            self,
            "Protected",
            table_name=f"{prefix}-protected",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
