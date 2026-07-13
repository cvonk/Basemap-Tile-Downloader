# -*- coding: utf-8 -*-
"""
safexml.py – a hardened, dependency-free replacement for
xml.etree.ElementTree.fromstring for parsing XML fetched from remote servers.

The WMS/WMTS backends parse GetCapabilities and ServiceException responses that
come from a user-chosen (hence untrusted) server. Plain ElementTree is open to
XML entity-expansion ("billion laughs") and external-entity (XXE) attacks. The
usual remedy, defusedxml, is NOT bundled with QGIS, and a QGIS plugin can't
reliably install extra packages — so this module hardens the standard-library
expat parser instead, mirroring defusedxml's default protections:

  * entity *definitions* (`<!ENTITY …>`) are rejected  → no billion-laughs
  * external entities / parameter entities are never parsed → no XXE / no network
  * a DOCTYPE itself is tolerated (WMS 1.1.1 capabilities legitimately carry one)

It builds the tree via expat + ElementTree.TreeBuilder, reproducing
ElementTree's "{namespace}local" tag naming, so existing callers (which strip
the namespace by splitting on "}") keep working unchanged.
"""

import xml.parsers.expat as _expat
# TreeBuilder only assembles nodes from the (hardened) expat parser's callbacks;
# entity definitions are rejected and external entities are never resolved (see
# fromstring below), so this is NOT the vulnerable ElementTree parsing path.
from xml.etree.ElementTree import TreeBuilder, ParseError  # nosec B405


def _qname(name):
    """expat (namespace_separator='}') yields 'uri}local' for namespaced names
    and 'local' otherwise; ElementTree formats these as '{uri}local'."""
    return "{" + name if "}" in name else name


def fromstring(data):
    """Parse XML `data` (bytes or str) into an Element, hardened against
    entity-expansion and external-entity attacks. Raises
    xml.etree.ElementTree.ParseError on malformed or disallowed input, so
    callers can keep catching ParseError exactly as with ElementTree."""
    if isinstance(data, str):
        data = data.encode("utf-8")

    builder = TreeBuilder()
    parser = _expat.ParserCreate(None, "}")     # namespace-aware, like ElementTree
    # Never parse parameter entities or the external DTD subset: no network
    # fetches, so external-entity (XXE) attacks are impossible.
    try:
        parser.SetParamEntityParsing(_expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except (AttributeError, ValueError):
        pass

    def _forbid_entity(*_args):
        raise ParseError("entity definitions are not allowed")

    # Reject any in-document entity definition — this is what stops a
    # "billion laughs" payload from being expanded.
    parser.EntityDeclHandler = _forbid_entity
    # Refuse to resolve any external entity reference (belt-and-suspenders).
    parser.ExternalEntityRefHandler = lambda *_a: 0

    def _start(name, attrs):
        builder.start(_qname(name), {_qname(k): v for k, v in attrs.items()})

    parser.buffer_text = True
    parser.StartElementHandler = _start
    parser.EndElementHandler = lambda name: builder.end(_qname(name))
    parser.CharacterDataHandler = builder.data

    try:
        parser.Parse(data, True)
    except _expat.ExpatError as e:
        raise ParseError(str(e))
    return builder.close()
