from hashlib import sha256
import json
import re
import pytest

from lifedata_ai.reference_templates import MASTER, MASTER_SHA256, build_reference_document, validate_reference_template
from lifedata_ai.tools.base import ToolError


def test_source_is_immutable_and_live_adapter_never_activates_sample_script():
    before = MASTER.read_bytes()
    title = '</script><script>alert("bad")</script>'
    document = build_reference_document("a-publication", title, {"residents": {"chart_ids": [7]}})
    assert sha256(before).hexdigest() == MASTER_SHA256
    assert MASTER.read_bytes() == before
    assert '<script src="./support.js">' not in document
    assert 'type="text/x-dc" data-dc-script' in document
    assert title not in document
    data = json.loads(re.search(r'<script id="vima-reference-data" type="application/json">(.*?)</script>', document).group(1))
    assert data["title"] == title and data["mode"] == "live"
    assert data["bindings"] == {"residents": {"chart_ids": [7]}}


def test_binding_membership_and_slot_names_are_authoritative():
    config = {"template": "sincep-hdss", "bindings": {"residents": {"chart_ids": [7, 9], "encoding": {"metrics": ["population"], "number_format": ",d"}}}}
    validate_reference_template(config, {7, 9})
    with pytest.raises(ToolError, match="every accessible source"):
        validate_reference_template(config, {7})
    with pytest.raises(ToolError, match="Invalid reference"):
        validate_reference_template({"template": "sincep-hdss", "bindings": {"invented": {"chart_ids": [7]}}}, {7})


def test_preview_is_labeled_and_uses_original_runtime_without_mutating_source():
    document = build_reference_document()
    assert '"mode": "illustrative_preview"' in document
    assert '/lifedata-ai/static/dashboards/reference.js' in document
