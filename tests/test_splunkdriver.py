# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
from pytest import raises
from ..msticpy.data.data_providers import QueryProvider
from ..msticpy.data.drivers import SplunkDriver
from ..msticpy.common.utility import MsticpyException


def test_splunk():
    splunk_prov = QueryProvider(data_environment='Splunk')
    assert type(splunk_prov) == QueryProvider
    with raises(ConnectionError):
        splunk_prov.connect(host="splunkbots.westus2.cloudapp.azure.com", username="admin", password="SplunkADM!!")
    with raises(MsticpyException):
        splunk_prov.connect(app_name="Splunk")
        self.assertTrue(qry_prov.connected)
        queries = qry_prov.list_queries()
        self.assertGreaterEqual(len(queries), 8)
        self.assertIn("SecurityAlert.list_alerts", queries)
        self.assertIn("WindowsSecurity.list_host_events", queries)
        self.assertIn("Network.list_azure_network_flows_by_ip", queries)    
