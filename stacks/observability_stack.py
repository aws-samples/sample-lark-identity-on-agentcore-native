"""Observability stack — CloudWatch dashboard + alarms.

Kept intentionally light for a PoC: a dashboard with Lambda error/duration and
AgentCore invocation widgets, plus an SNS-less error alarm. Deployed last and
depends on nothing (references log/metric namespaces by name).
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_cloudwatch as cw,
)
from constructs import Construct


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"

        def lambda_metric(fn_name: str, metric: str, stat: str = "Sum") -> cw.Metric:
            return cw.Metric(
                namespace="AWS/Lambda", metric_name=metric,
                dimensions_map={"FunctionName": fn_name},
                statistic=stat, period=Duration.minutes(5),
            )

        functions = [f"{prefix}-router", f"{prefix}-web-api",
                     f"{prefix}-interceptor", f"{prefix}-demo-tool"]

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{prefix}-ops")
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda errors",
                left=[lambda_metric(f, "Errors") for f in functions],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda duration (p95, ms)",
                left=[lambda_metric(f, "Duration", "p95") for f in functions],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda invocations",
                left=[lambda_metric(f, "Invocations") for f in functions],
                width=24,
            ),
        )

        # Router errors alarm (webhook path) — the one most worth paging on.
        self.router_error_alarm = cw.Alarm(
            self, "RouterErrorAlarm",
            alarm_name=f"{prefix}-router-errors",
            metric=lambda_metric(f"{prefix}-router", "Errors"),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        CfnOutput(self, "DashboardName", value=dashboard.dashboard_name)
