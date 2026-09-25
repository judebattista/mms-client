"""An IEC 61850 IED client for verifying and troubleshooting IEDs in a test rack (IED-CLIENT-SPEC.md).

The protocol it speaks to a device comes from a protocol module (see :mod:`ied_client.protocol`).
"""

__version__ = "0.1.0"

# The product's name: the command, the JSON envelope's tool name, the state directory. Formats that are
# stored in files (snapshot and incident ``kind``) keep their own fixed identifiers.
TOOL_NAME = "mms-client"
ENV_PREFIX = "MMS_CLIENT"  # environment variables: <ENV_PREFIX>_INVENTORY, <ENV_PREFIX>_LOG_DIR
