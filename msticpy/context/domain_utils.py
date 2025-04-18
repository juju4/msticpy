# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
"""
Functions to support investigation of a domain or url.

Includes functions to conduct common investigation steps when dealing
with a domain or url, such as getting a screenshot or validating the TLD.

"""
from __future__ import annotations

import datetime as dt
import json
import logging
import ssl
import time
from dataclasses import asdict
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable
from urllib.error import HTTPError, URLError

import httpx
import pandas as pd
import tldextract
from cryptography import x509

# CodeQL [SM02167] Compatibility requirement for SSL abuse list
from cryptography.hazmat.primitives.hashes import SHA1  # CodeQL [SM02167] Compatibility
from dns.exception import DNSException
from dns.resolver import Resolver
from IPython import display
from ipywidgets import IntProgress
from typing_extensions import Self
from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

from .._version import VERSION
from ..common.exceptions import MsticpyUserConfigError
from ..common.settings import get_config, get_http_timeout
from ..common.utility import export, mp_ua_header

if TYPE_CHECKING:
    from cryptography.x509 import Certificate
    from dns.resolver import Answer
    from tldextract.tldextract import ExtractResult
__version__ = VERSION
__author__ = "Pete Bryan"
logger: logging.Logger = logging.getLogger(__name__)


@export
def screenshot(  # pylint: disable=too-many-locals
    url: str,
    api_key: str | None = None,
    *,
    sleep: float = 0.05,
    max_progress: int = 100,
) -> httpx.Response:
    """
    Get a screenshot of a url with Browshot.

    Parameters
    ----------
    url : str
        The url a screenshot is wanted for.
    api_key : str (optional)
        Browshot API key. If not set msticpyconfig checked for this.
    sleep: int (optional)
        Time to sleep between calls. Defaults to 0.05 seconds
    max_progress: int (optional)
        Set the maximum value for the progress bar. Defaults to 100.

    Returns
    -------
    image_data: httpx.Response
        The final screenshot request response data.

    """
    # Get Browshot API key from kwargs or config
    if api_key is not None:
        bs_api_key: str | None = api_key
    else:
        bs_conf: dict[str, Any] = get_config(
            "DataProviders.Browshot",
            {},
        ) or get_config(
            "Browshot",
            {},
        )
        bs_api_key = None
        if bs_conf is not None:
            bs_api_key = bs_conf.get("Args", {}).get("AuthKey")

    if bs_api_key is None:
        err_msg: str = (
            "No configuration found for Browshot\n"
            "Please add a section to msticpyconfig.yaml:\n"
            "DataProviders:\n"
            "  Browshot:\n"
            "    Args:\n"
            "      AuthKey: {your_auth_key}"
        )
        raise MsticpyUserConfigError(
            err_msg,
            title="Browshot configuration not found",
            browshot_uri=("Get an API key for Browshot", "https://api.browshot.com/"),
        )

    # Request screenshot from Browshot and get request ID
    id_string: str = (
        f"https://api.browshot.com/api/v1/screenshot/create?url={url}/"
        f"&instance_id=26&size=screen&cache=0&key={bs_api_key}"
    )
    id_data: httpx.Response = httpx.get(
        id_string,
        timeout=get_http_timeout(),
        headers=mp_ua_header(),
    )
    bs_id: str = json.loads(id_data.content)["id"]
    status_string: str = (
        f"https://api.browshot.com/api/v1/screenshot/info?id={bs_id}&key={bs_api_key}"
    )
    image_string: str = (
        f"https://api.browshot.com/api/v1/screenshot/thumbnail?id={bs_id}"
        f"&zoom=50&key={bs_api_key}"
    )
    # Wait until the screenshot is ready and keep user updated with progress
    logger.info("Getting screenshot")
    progress = IntProgress(min=0, max=max_progress)
    display.display(progress)
    ready = False
    while not ready and progress.value < max_progress:
        progress.value += 1
        status_data: httpx.Response = httpx.get(
            status_string,
            timeout=get_http_timeout(),
            headers=mp_ua_header(),
        )
        status: str = json.loads(status_data.content)["status"]
        if status == "finished":
            ready = True
        else:
            time.sleep(sleep)
    progress.value = max_progress

    # Once ready or timed out get the screenshot
    image_data: httpx.Response = httpx.get(image_string, timeout=get_http_timeout())

    if not image_data.is_success:
        logger.warning(
            "There was a problem with the request, please check the status code for details",
        )

    return image_data


# Backward compat with dnspython 1.x
# If v2.x installed use non-deprecated "resolve" method
# otherwise use "query"
_dns_resolver = Resolver()
if hasattr(_dns_resolver, "resolve"):
    _dns_resolve: Callable[..., Answer] = _dns_resolver.resolve
else:
    _dns_resolve = _dns_resolver.query


@export
class DomainValidator:
    """Assess a domain's validity."""

    _ssl_abuse_list: pd.DataFrame = pd.DataFrame()

    @classmethod
    def _check_and_load_abuselist(cls: type[Self]) -> None:
        """Pull IANA TLD list and save to internal attribute."""
        if cls._ssl_abuse_list is None or cls._ssl_abuse_list.empty:
            cls._ssl_abuse_list = cls._get_ssl_abuselist()

    @property
    def ssl_abuse_list(self: Self) -> pd.DataFrame:
        """
        Return the class SSL Blacklist.

        Returns
        -------
        pd.DataFrame
            SSL Blacklist

        """
        self._check_and_load_abuselist()
        return self._ssl_abuse_list

    @staticmethod
    def validate_tld(url_domain: str) -> bool:
        """
        Validate if a domain's TLD is valid.

        Parameters
        ----------
        url_domain : str
            The url or domain to validate.

        Returns
        -------
        result:
            True if valid public TLD, False if not.

        """
        extract_result: ExtractResult = tldextract.extract(url_domain.lower())
        return bool(extract_result.suffix)

    @staticmethod
    def is_resolvable(url_domain: str) -> bool:
        """
        Validate if a domain or URL be be resolved to an IP address.

        Parameters
        ----------
        url_domain : str
            The url or domain to validate.

        Returns
        -------
        result:
            True if valid resolvable, False if not.

        """
        try:
            _dns_resolve(url_domain, "A")
        except DNSException:
            return False
        return True

    def in_abuse_list(self: Self, url_domain: str) -> tuple[bool, Certificate | None]:
        """
        Validate if a domain or URL's SSL cert the abuse.ch SSL Abuse List.

        Parameters
        ----------
        url_domain : str
            The url or domain to validate.

        Returns
        -------
        Tuple[bool, Optional[Certificate]]:
            True if valid in the list, False if not.
            Certificate - the certificate loaded from the domain.

        """
        try:
            cert: str = ssl.get_server_certificate((url_domain, 443))
            x509_cert: Certificate = x509.load_pem_x509_certificate(
                cert.encode("ascii"),
            )
            cert_sha1: bytes = x509_cert.fingerprint(
                SHA1()  # nosec
            )  # noqa: S303  # CodeQL [SM02167] Compatibility requirement for SSL abuse list
            result = bool(
                self.ssl_abuse_list["SHA1"].str.contains(cert_sha1.hex()).any(),
            )
        except Exception:  # pylint: disable=broad-except
            return False, None
        return result, x509_cert

    @classmethod
    def _get_ssl_abuselist(cls: type[Self]) -> pd.DataFrame:
        """Download and load abuse.ch SSL Abuse List."""
        try:
            ssl_ab_list: pd.DataFrame = pd.read_csv(
                "https://sslbl.abuse.ch/blacklist/sslblacklist.csv",
                skiprows=8,
            )
        except (ConnectionError, HTTPError, URLError):
            ssl_ab_list = pd.DataFrame({"SHA1": []})

        return ssl_ab_list


def dns_components(domain: str) -> dict:
    """
    Return components of domain as dict.

    Parameters
    ----------
    domain : str
        The domain to extract.

    Returns
    -------
    dict:
        Returns subdomain and TLD components from a domain.

    """
    result: ExtractResult = tldextract.extract(domain.lower())
    if isinstance(result, tuple) and hasattr(result, "_asdict"):
        return result._asdict()
    return asdict(result)


def url_components(url: str) -> dict[str, str]:
    """Return parsed Url components as dict."""
    try:
        return parse_url(url)._asdict()
    except LocationParseError:
        return {}


@export
def dns_resolve(url_domain: str, rec_type: str = "A") -> dict[str, Any]:
    """
    Validate if a domain or URL be be resolved to an IP address.

    Parameters
    ----------
    url_domain : str
        The url or domain to validate.
    rec_type : str
        The DNS record type to query, by default "A"

    Returns
    -------
    Dict[str, Any]:
        Resolver result as dictionary.

    """
    domain: str | None = parse_url(url_domain).host
    if not domain:
        err_msg: str = f"Failed to parse url: {url_domain}"
        raise ValueError(err_msg)
    try:
        return _resolve_resp_to_dict(_dns_resolve(domain, rdtype=rec_type))
    except DNSException as err:
        return {
            "qname": domain,
            "rdtype": rec_type,
            "response": str(err),
        }


@export
def dns_resolve_df(url_domain: str, rec_type: str = "A") -> pd.DataFrame:
    """
    Validate if a domain or URL be be resolved to an IP address.

    Parameters
    ----------
    url_domain : str
        The url or domain to validate.
    rec_type : str
        The DNS record type to query, by default "A"

    Returns
    -------
    pd.DataFrame:
        Resolver result as dataframe with individual resolution
        results as separate rows.

    """
    results = pd.DataFrame([dns_resolve(url_domain, rec_type)])
    if "rrset" in results.columns:
        return results.explode(column="rrset")
    return results


@export
def ip_rev_resolve(ip_address: str) -> dict[str, Any]:
    """
    Reverse lookup for IP Address.

    Parameters
    ----------
    ip_address : str
        The IP address to query.

    Returns
    -------
    Dict[str, Any]:
        Resolver result as dictionary.

    """
    try:
        return _resolve_resp_to_dict(_dns_resolve(ip_address, raise_on_no_answer=True))
    except DNSException as err:
        return {
            "qname": ip_address,
            "rdtype": "PTR",
            "response": str(err),
        }


@export
def ip_rev_resolve_df(ip_address: str) -> pd.DataFrame:
    """
    Reverse lookup for IP Address.

    Parameters
    ----------
    ip_address : str
        The IP address to query.

    Returns
    -------
    pd.DataFrame:
        Resolver result as dataframe with individual resolution
        results as separate rows.

    """
    results = pd.DataFrame([ip_rev_resolve(ip_address)])
    if "rrset" in results.columns:
        return results.explode(column="rrset")
    return results


@export
def _resolve_resp_to_dict(resolver_resp: Answer) -> dict[str, Any]:
    """Return Dns Python resolver response to dict."""
    rdtype: str = (
        resolver_resp.rdtype.name
        if isinstance(resolver_resp.rdtype, Enum)
        else str(resolver_resp.rdtype)
    )
    rdclass: str = (
        resolver_resp.rdclass.name
        if isinstance(resolver_resp.rdclass, Enum)
        else str(resolver_resp.rdclass)
    )

    result: dict[str, Any] = {
        "qname": str(resolver_resp.qname),
        "rdtype": rdtype,
        "rdclass": rdclass,
        "response": str(resolver_resp.response),
        "nameserver": getattr(resolver_resp, "nameserver", None),
        "port": getattr(resolver_resp, "port", None),
        "canonical_name": str(resolver_resp.canonical_name),
        "expiration": dt.datetime.fromtimestamp(
            resolver_resp.expiration,
            tz=dt.timezone.utc,
        ),
    }
    if resolver_resp.rrset:
        result["rrset"] = [str(res) for res in resolver_resp.rrset]
    return result
