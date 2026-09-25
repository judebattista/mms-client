"""The MMS protocol module for the IED client (MMS-PROTOCOL-SPEC.md), on pyiec61850-ng / libiec61850.

``protocol`` is the object registered under the ``ied_client.protocols`` entry point.
"""

from .module import MmsProtocol

protocol = MmsProtocol()

__all__ = ["MmsProtocol", "protocol"]
