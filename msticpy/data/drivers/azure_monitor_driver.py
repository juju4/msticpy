# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
"""
Azure monitor/Log Analytics KQL Driver class.

See Also
--------
Azure SDK code: https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/monitor

Azure SDK docs: https://learn.microsoft.com/python/api/overview/
azure/monitor-query-readme?view=azure-python

"""
from __future__ import annotations

import contextlib
import logging
import warnings
from datetime import datetime
from typing import Any, Iterable, cast

import httpx
import pandas as pd
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.policies import UserAgentPolicy
from packaging.version import Version
from packaging.version import parse as parse_version

from ..._version import VERSION
from ...auth.azure_auth import AzureCloudConfig, az_connect
from ...common.exceptions import (
    MsticpyDataQueryError,
    MsticpyKqlConnectionError,
    MsticpyMissingDependencyError,
    MsticpyNoDataSourceError,
    MsticpyNotConnectedError,
)
from ...common.provider_settings import get_protected_setting
from ...common.settings import get_http_proxies, get_http_timeout
from ...common.timespan import TimeSpan
from ...common.utility import export, mp_ua_header
from ...common.wsconfig import WorkspaceConfig
from ..core.query_defns import DataEnvironment
from .driver_base import DriverBase, DriverProps, QuerySource

logger = logging.getLogger(__name__)

# pylint: disable=ungrouped-imports
try:
    from azure.monitor.query import (
        LogsQueryClient,
        LogsQueryPartialResult,
        LogsQueryResult,
    )
    from azure.monitor.query import __version__ as az_monitor_version
except ImportError as imp_err:
    raise MsticpyMissingDependencyError(
        "Cannot use this feature without Azure monitor client installed",
        title="Error importing azure.monitor.query",
        packages="azure-monitor-query",
    ) from imp_err

__version__ = VERSION
__author__ = "Ian Hellen"

_HELP_URL = (
    "https://msticpy.readthedocs.io/en/latest/data_acquisition/DataProv-MSSentinel.html"
)
# pylint: disable=too-many-instance-attributes


@export
class AzureMonitorDriver(DriverBase):
    """KqlDriver class to execute kql queries."""

    _DEFAULT_TIMEOUT = 300

    def __init__(self, connection_str: str | None = None, **kwargs):
        """
        Instantiate KqlDriver and optionally connect.

        Parameters
        ----------
        connection_str : str, optional
            Connection string

        Other Parameters
        ----------------
        debug : bool
            print out additional diagnostic information.
        timeout : int (seconds)
            Specify a timeout for queries. Default is 300 seconds.
            (can be set here or in connect and overridden in query methods)
        proxies : dict[str, str]
            Proxy settings for log analytics queries.
            Dictionary format is {protocol: proxy_url}
            Where protocol is https, http, etc. and proxy_url can contain
            optional authentication information in the format
            "https://username:password@proxy_host:port"
            If you have a proxy configuration in msticpyconfig.yaml and
            you do not want to use it, set this to an empty dictionary.
            (can be overridden in connect method)

        """
        if kwargs.get("debug", False):
            logger.setLevel(logging.DEBUG)
        super().__init__(**kwargs)

        self._schema: dict[str, Any] = {}
        self.set_driver_property(
            DriverProps.FORMATTERS,
            {"datetime": self._format_datetime, "list": self._format_list},
        )
        self._loaded = True
        self._ua_policy = UserAgentPolicy(user_agent=mp_ua_header()["UserAgent"])
        self._def_timeout = kwargs.get(
            "timeout", kwargs.get("server_timeout", self._DEFAULT_TIMEOUT)
        )
        self._def_proxies = kwargs.get("proxies", get_http_proxies())
        self._query_client: LogsQueryClient | None = None
        self._az_tenant_id: str | None = None
        self._ws_config: WorkspaceConfig | None = None
        self._ws_name: str | None = None
        self._workspace_id: str | None = None
        self._workspace_ids: list[str] = []
        self._def_connection_str: str | None = connection_str
        self._connect_auth_types: list[str] | None = None
        self._try_get_schema: bool = True
        self._fail_on_partial: bool = kwargs.get("fail_on_partial", False)
        self.add_query_filter(
            "data_environments", ("MSSentinel", "LogAnalytics", "AzureSentinel")
        )
        self.set_driver_property(
            DriverProps.EFFECTIVE_ENV, DataEnvironment.MSSentinel.name
        )
        self.set_driver_property(DriverProps.SUPPORTS_THREADING, value=True)
        self.set_driver_property(
            DriverProps.MAX_PARALLEL, value=kwargs.get("max_threads", 4)
        )
        self.az_cloud_config = AzureCloudConfig()
        logger.info(
            "AzureMonitorDriver loaded. connect_str  %s, kwargs: %s",
            connection_str,
            kwargs,
        )

    @property
    def url_endpoint(self) -> str:
        """Return the current URL endpoint for Azure Monitor."""
        base_url = self.az_cloud_config.log_analytics_uri
        # post v1.1.0 of azure-monitor-query, the API version requires a 'v1' suffix
        if parse_version(az_monitor_version) > parse_version("1.1.0"):
            return f"{base_url}v1"
        return base_url

    @property
    def current_connection(self) -> str:
        """Return the current connection name."""
        connection = self._ws_name
        if (
            not connection
            and self._ws_config
            and WorkspaceConfig.CONF_WS_NAME_KEY in self._ws_config
        ):
            connection = self._ws_config[WorkspaceConfig.CONF_WS_NAME_KEY]
        return (
            connection
            or self._def_connection_str
            or self._workspace_id
            or next(iter(self._workspace_ids), "")
            or "AzureMonitor"
        )

    @current_connection.setter
    def current_connection(self, value: str):
        """Allow attrib to be set but ignore."""
        del value

    def connect(self, connection_str: str | WorkspaceConfig | None = None, **kwargs):
        """
        Connect to data source.

        Parameters
        ----------
        connection_str : str | WorkspaceConfig | None
            Connection string or WorkspaceConfig for the Sentinel Workspace.

        Other Parameters
        ----------------
        auth_types: Iterable [str]
            Authentication (credential) types to use. By default the
            values configured in msticpyconfig.yaml are used. If not set,
            it will use the msticpy defaults.
        mp_az_auth : bool | str | list | None, optional
            Deprecated parameter directing driver to use MSTICPy Azure authentication.
            Values can be:
            True or "default": use the settings in msticpyconfig.yaml 'Azure' section
            str: single auth method name
            list[str]: list of acceptable auth methods from
            Use `auth_types` parameter instead.
        tenant_id: str, optional
            Optional parameter specifying a Tenant ID for use by MSTICPy Azure
            authentication. By default, the tenant_id for the workspace.
        workspace : str, optional
            Alternative to supplying a WorkspaceConfig object as the connection_str
            parameter. Giving a workspace name will fetch the workspace
            settings from msticpyconfig.yaml.
        workspaces : Iterable[str], optional
            List of workspaces to run the queries against, each workspace name
            must have an entry in msticpyconfig.yaml
        workspace_ids: Iterable[str], optional
            List of workspace IDs to run the queries against. Must be supplied
            along with a `tenant_id`.
        timeout : int (seconds)
            Specify a timeout for queries. Default is 300 seconds.
            (can be overridden query method)
        proxies : dict[str, str]
            Proxy settings for log analytics queries.
            Dictionary format is {protocol: proxy_url}
            Where protocol is https, http, etc. and proxy_url can contain
            optional authentication information in the format
            "https://username:password@proxy_host:port"
            If you have a proxy configuration in msticpyconfig.yaml and
            you do not want to use it, set this to an empty dictionary.
        fail_on_partial: bool
            Fail queries if only partial results are returned.

        Notes
        -----
        When using the `workspaces` or `workspace_ids` parameters, some
        functionality will be reduced - e.g. no schema will be available for
        the workspaces. As an alternative to using multiple workspaces here
        you can create multiple workspace connections

        """
        self._connected = False
        self._query_client = self._create_query_client(connection_str, **kwargs)

        self._fail_on_partial = kwargs.get("fail_on_partial", self._fail_on_partial)
        # get the schema
        if self._try_get_schema:
            self._schema = self._get_schema()
        self._connected = True
        print("connected")

        return self._connected

    # pylint: disable=too-many-branches

    @property
    def schema(self) -> dict[str, dict]:
        """
        Return current data schema of connection.

        Returns
        -------
        dict[str, dict]
            Data schema of current connection.

        """
        return self._schema

    def query(
        self, query: str, query_source: QuerySource | None = None, **kwargs
    ) -> pd.DataFrame | Any:
        """
        Execute query string and return DataFrame of results.

        Parameters
        ----------
        query : str
            The query to execute
        query_source : QuerySource | None
            The query definition object

        Other Parameters
        ----------------
        timeout : int (seconds)
            Specify a timeout for the query. Default is 300 seconds.
        fail_on_partial: bool
            Fail the query if only partial results are returned.

        Returns
        -------
        pd.DataFrame | Any
            A DataFrame (if successful) or
            the underlying provider result if an error.

        """
        if not self._connected or self._query_client is None:
            raise MsticpyNotConnectedError(
                "Please run connect() to connect to the workspace",
                "before running a query.",
                title="Workspace not connected.",
                help_uri=_HELP_URL,
            )
        if query_source:
            self._check_table_exists(query_source)
        data, result = self.query_with_results(query, **kwargs)
        return data if data is not None else result

    # pylint: disable=too-many-branches
    def query_with_results(
        self, query: str, **kwargs
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """
        Execute query string and return DataFrame of results.

        Parameters
        ----------
        query : str
            The kql query to execute

        Returns
        -------
        tuple[pd.DataFrame, dict[str, Any]]
            A DataFrame (if successful) and
            Query status dictionary.

        """
        if not self._connected or self._query_client is None:
            raise MsticpyNotConnectedError(
                "Please run connect() to connect to the workspace",
                "before running a query.",
                title="Workspace not connected.",
                help_uri=_HELP_URL,
            )
        time_span_value = self._get_time_span_value(**kwargs)
        fail_on_partial = kwargs.get(
            "fail_if_partial", kwargs.get("fail_on_partial", self._fail_on_partial)
        )
        server_timeout = kwargs.pop("timeout", self._def_timeout)

        workspace_id = next(iter(self._workspace_ids), None) or self._workspace_id
        additional_workspaces = self._workspace_ids[1:] if self._workspace_ids else None
        logger.info("Query to run %s", query)
        logger.info(
            "Workspaces %s", ",".join(self._workspace_ids) or self._workspace_id
        )
        logger.info(
            "Time span %s - %s",
            str(time_span_value[0]) if time_span_value else "none",
            str(time_span_value[1]) if time_span_value else "none",
        )
        logger.info("Timeout %s", server_timeout)
        try:
            result = self._query_client.query_workspace(
                workspace_id=workspace_id,  # type: ignore[arg-type]
                query=query,
                timespan=time_span_value,  # type: ignore[arg-type]
                server_timeout=server_timeout,
                additional_workspaces=additional_workspaces,
            )
        except HttpResponseError as http_err:
            result = None
            self._raise_query_failure(query, http_err)
        # We might get an unknown exception type from azure.monitor.query
        except Exception as unknown_err:  # pylint: disable=broad-except
            result = None
            self._raise_unknown_error(unknown_err)
        result = cast(LogsQueryResult, result)
        status = self._get_query_status(result)
        logger.info("query status %s", repr(status))

        if isinstance(result, LogsQueryPartialResult):
            if fail_on_partial:
                raise MsticpyDataQueryError(
                    "Partial results returned. This may indicate a query timeout.",
                    title="Partial results returned",
                    help_uri=_HELP_URL,
                )
            warnings.warn(
                "Partial results returned. This may indicate a query timeout.",
                RuntimeWarning,
            )
            table = result.partial_data[0]  # type: ignore[attr-defined]
        else:
            table = result.tables[0]  # type: ignore[attr-defined]
        data_frame = pd.DataFrame(table.rows, columns=table.columns)
        logger.info("Dataframe returned with %d rows", len(data_frame))
        return data_frame, status

    def _create_query_client(self, connection_str, **kwargs):
        """Create the query client."""
        az_auth_types = kwargs.pop("auth_types", kwargs.get("mp_az_auth"))
        if isinstance(az_auth_types, bool):
            az_auth_types = None
        if isinstance(az_auth_types, str):
            az_auth_types = [az_auth_types]
        self._connect_auth_types = az_auth_types

        self._def_timeout = kwargs.pop("timeout", self._DEFAULT_TIMEOUT)
        self._def_proxies = kwargs.pop("proxies", self._def_proxies)
        self._get_workspaces(connection_str, **kwargs)

        # check for additional Args in settings but allow kwargs to override
        connect_args = self._get_workspace_settings_args()
        connect_args.update(kwargs)
        connect_args.update(
            {"auth_methods": az_auth_types, "tenant_id": self._az_tenant_id}
        )
        credentials = az_connect(**connect_args)
        logger.info(
            "Created query client. Auth type: %s, Url: %s, Proxies: %s",
            type(credentials.modern) if credentials else "None",
            self.url_endpoint,
            kwargs.get("proxies", self._def_proxies),
        )
        return LogsQueryClient(
            credential=credentials.modern,
            endpoint=self.url_endpoint,
            proxies=kwargs.get("proxies", self._def_proxies),
        )

    def _get_workspace_settings_args(self) -> dict[str, Any]:
        """Return any Args settings for the current workspace."""
        if not self._ws_config or not self._ws_config.settings_path:
            return {}
        args_path = f"{self._ws_config.settings_path}.Args"
        args_settings = self._ws_config.settings.get("Args", {})
        return {
            name: get_protected_setting(args_path, name)
            for name in args_settings.keys()
        }

    def _get_workspaces(self, connection_str: str | None = None, **kwargs):
        """Get workspace or workspaces to connect to."""
        self._az_tenant_id = kwargs.get("tenant_id", kwargs.get("mp_az_tenant_id"))
        # multiple workspace IDs
        if workspaces := kwargs.pop("workspaces", None):
            self._get_workspaces_by_name(workspaces)
            return
        if workspace_ids := kwargs.pop("workspace_ids", None):
            self._get_workspaces_by_id(workspace_ids)
            return

        # standard - single-workspace configuration
        workspace_name = kwargs.get("workspace")
        ws_config: WorkspaceConfig | None = None
        connection_str = connection_str or self._def_connection_str
        if workspace_name or connection_str is None:
            ws_config = WorkspaceConfig(workspace=workspace_name)  # type: ignore
            logger.info(
                "WorkspaceConfig created from workspace name %s", workspace_name
            )
        elif isinstance(connection_str, str):
            self._def_connection_str = connection_str
            with contextlib.suppress(ValueError):
                ws_config = WorkspaceConfig.from_connection_string(connection_str)
                logger.info(
                    "WorkspaceConfig created from connection_str %s", connection_str
                )
        elif isinstance(connection_str, WorkspaceConfig):
            logger.info("WorkspaceConfig as parameter %s", connection_str.workspace_id)
            ws_config = connection_str

        if not ws_config:
            logger.warning("No workspace set")
            raise MsticpyKqlConnectionError(
                "A workspace name, config or connection string is needed"
                " to connect to a workspace.",
                title="No connection details",
                help_uri=_HELP_URL,
            )
        if ws_config.workspace_id is None or ws_config.tenant_id is None:
            logger.warning("Unable to get workspace ID or tenant ID")
            raise MsticpyKqlConnectionError(
                "The workspace config or connection string did not have"
                "the required parameters to connect to a workspace.",
                "At least a workspace ID and tenant ID are required.",
                title="No connection details",
                help_uri=_HELP_URL,
            )
        self._ws_config = ws_config
        self._ws_name = workspace_name or ws_config.workspace_id
        if not self._az_tenant_id and WorkspaceConfig.CONF_TENANT_ID in ws_config:
            self._az_tenant_id = ws_config[WorkspaceConfig.CONF_TENANT_ID]
        self._workspace_id = ws_config[WorkspaceConfig.CONF_WS_ID]

    def _get_workspaces_by_id(self, workspace_ids):
        if not self._az_tenant_id:
            raise MsticpyKqlConnectionError(
                "You must supply a tenant_id with the workspace_ids parameter",
                title="No tenant_id supplied.",
                help_uri=_HELP_URL,
            )
        self._workspace_ids = workspace_ids
        logger.info(
            "%d configured workspaces: %s",
            len(self._workspace_ids),
            ", ".join(self._workspace_ids),
        )

    def _get_workspaces_by_name(self, workspaces):
        workspace_configs = {
            WorkspaceConfig(workspace)[WorkspaceConfig.CONF_WS_ID]: WorkspaceConfig(
                workspace
            )[WorkspaceConfig.CONF_TENANT_ID]
            for workspace in workspaces
        }
        if len(set(workspace_configs.values())) > 1:
            raise ValueError("All workspaces must have the same tenant ID.")
        self._az_tenant_id = next(iter(workspace_configs.values()))
        self._workspace_ids = list(set(workspace_configs))
        logger.info(
            "%d configured workspaces: %s",
            len(self._workspace_ids),
            ", ".join(self._workspace_ids),
        )

    def _get_time_span_value(self, **kwargs):
        """Return the timespan for the query API call."""
        default_time_params = kwargs.get("default_time_params", False)
        time_params = kwargs.get("time_span", {})
        start = time_params.get("start")
        end = time_params.get("end")
        if default_time_params or (start is None or end is None):
            time_span_value = None
            logger.info("No time parameters supplied.")
        else:
            time_span = TimeSpan(
                start=time_params["start"],
                end=time_params["end"],
            )
            # Azure Monitor API expects datetime objects, so
            # convert to datetimes if we have pd.Timestamps
            t_start = (
                time_span.start.to_pydatetime(warn=False)
                if isinstance(time_span.start, pd.Timestamp)
                else time_span.start
            )
            t_end = (
                time_span.end.to_pydatetime(warn=False)
                if isinstance(time_span.end, pd.Timestamp)
                else time_span.end
            )
            time_span_value = t_start, t_end
            logger.info("Time parameters set %s", str(time_span))
        return time_span_value

    def _check_table_exists(self, query_source):
        """Check that query table is in the workspace schema."""
        if not self.schema:
            return
        try:
            table = query_source.params.get("table", {}).get("default")
        except KeyError:
            table = None
        if table:
            if " " in table.strip():
                table = table.strip().split(" ")[0]
            if table not in self.schema:
                raise MsticpyNoDataSourceError(
                    f"The table {table} for this query is not in your workspace",
                    "or database schema. Please check your query.",
                    title=f"{table} not found.",
                    help_uri=_HELP_URL,
                )

    @staticmethod
    def _get_query_status(result) -> dict[str, Any]:
        if isinstance(result, LogsQueryResult):
            return {
                "status": result.status.name,
                "tables": len(result.tables),
            }
        if isinstance(result, LogsQueryPartialResult):
            return {
                "status": result.status.name,
                "tables": len(result.partial_data),
            }
        return {"status": "unknown failure", "tables": 0, "result": result}

    def _get_schema(self) -> dict[str, dict]:
        """Return the workspace schema."""
        if not self._ws_config:
            logger.info("No workspace config - cannot get schema")
            return {}
        mgmt_endpoint = self.az_cloud_config.resource_manager

        url_tables = (
            "{endpoint}subscriptions/{sub_id}/resourcegroups/"
            "{res_group}/providers/Microsoft.OperationalInsights/workspaces/"
            "{ws_name}/tables?api-version=2021-12-01-preview"
        )
        try:
            ws_name = self._ws_config.workspace_name
        except AttributeError:
            ws_name = self._ws_config.workspace_key

        if not ws_name or ws_name == "Default":
            logger.info("No workspace name - cannot get schema")
            return {}
        try:
            fmt_url = url_tables.format(
                endpoint=mgmt_endpoint,
                sub_id=self._ws_config.subscription_id,
                res_group=self._ws_config.resource_group,
                ws_name=self._ws_config.workspace_name,
            )
        except AttributeError:
            logger.info("Not all workspace config available - cannot get schema")
            return {}

        credentials = az_connect(
            auth_methods=self._connect_auth_types, tenant_id=self._az_tenant_id
        )
        token = credentials.modern.get_token(f"{mgmt_endpoint}/.default")
        headers = {"Authorization": f"Bearer {token.token}", **mp_ua_header()}
        logger.info("Schema request to %s", fmt_url)

        # Handle proxies (parameter changes in httpx 0.25.0)
        httpx_version: Version = parse_version(httpx.__version__)
        proxies: dict[str, str] = self._def_proxies or {}
        httpx_proxy_kwargs: dict[str, Any] = {}
        if proxies:
            if httpx_version < parse_version("0.25.0"):
                httpx_proxy_kwargs = {"proxies": proxies}
            else:
                httpx_proxy_kwargs = {"mounts": proxies}
        with httpx.Client(
            timeout=get_http_timeout(),
            **httpx_proxy_kwargs,
        ) as httpx_client:
            response: httpx.Response = httpx_client.get(
                fmt_url,
                headers=headers,
            )
        if response.status_code != httpx.codes.OK:
            logger.info("Schema request failed. Status code: %d", response.status_code)
            return {}
        tables = response.json()
        logger.info(
            "Schema retrieved from workspace. %d tables found.",
            len(tables.get("value", 0)),
        )
        return _schema_format_tables(tables)

    @staticmethod
    def _format_datetime(date_time: datetime) -> str:
        """Return datetime-formatted string."""
        return date_time.isoformat(sep="T") + "Z"

    @staticmethod
    def _format_list(param_list: Iterable[Any]):
        """Return formatted list parameter."""
        fmt_list = []
        for item in param_list:
            if isinstance(item, str):
                fmt_list.append(f"'{item}'")
            else:
                fmt_list.append(f"{item}")
        return ", ".join(fmt_list)

    @staticmethod
    def _raise_query_failure(query, http_err):
        """Raise query failure exception."""
        err_contents = []
        if hasattr(http_err, "message"):
            err_contents = http_err.message.split("\n")
        if not err_contents:
            err_contents = ["Unknown query error"]

        err_contents.append(f"Query:\n{query}")
        raise MsticpyDataQueryError(
            *err_contents, title="Query Failure", help_uri=_HELP_URL
        ) from http_err

    @staticmethod
    def _raise_unknown_error(exception):
        """Raise an unknown exception."""
        raise MsticpyDataQueryError(
            "An unknown exception was returned by the service",
            *exception.args,
            f"Full exception:\n{exception}",
            title="connection failed",
            help_uri=_HELP_URL,
        ) from exception


def _schema_format_tables(
    ws_tables: dict[str, Iterable[dict[str, Any]]]
) -> dict[str, dict[str, str]]:
    """Return a sorted dictionary of table names and column names/types."""
    table_schema = {
        table["name"]: _schema_format_columns(table["properties"]["schema"])
        for table in ws_tables["value"]
    }
    return dict(sorted(table_schema.items()))


def _schema_format_columns(table_schema: dict[str, Any]) -> dict[str, str]:
    """Return a sorted dictionary of column names and types."""
    columns = {
        col["name"]: col["type"] for col in table_schema.get("standardColumns", {})
    }
    for col in table_schema.get("customColumns", []):
        columns[col["name"]] = col["type"]
    return dict(sorted(columns.items()))
