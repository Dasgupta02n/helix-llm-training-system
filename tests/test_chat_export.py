"""P3: gold export is LoRA/QLoRA chat-format JSONL."""

import json

from helix.services.library import gold_to_chat_messages


def test_chat_row_shape():
    row = gold_to_chat_messages("Where is my order?", "Share the order number.")
    assert list(row.keys()) == ["messages"]
    assert row["messages"][0] == {"role": "user", "content": "Where is my order?"}
    assert row["messages"][1] == {"role": "assistant", "content": "Share the order number."}
    line = json.dumps(row)
    parsed = json.loads(line)
    assert parsed["messages"][0]["role"] == "user"


def test_double_helix_zip_contains_chat_and_license():
    import zipfile
    from io import BytesIO

    from helix.services.double_helix import build_package_zip

    blob = build_package_zip(
        [{"input": "Hello?", "output": "Hi — how can I help?"}]
    )
    zf = zipfile.ZipFile(BytesIO(blob))
    names = zf.namelist()
    assert "data/train_chat.jsonl" in names
    assert "LICENSE.txt" in names
    assert "USAGE_AND_LIABILITY.txt" in names
    line = zf.read("data/train_chat.jsonl").decode().strip()
    row = json.loads(line)
    assert row["messages"][0]["role"] == "user"


def test_clamp_allows_100():
    from helix.services.pipeline_modes import clamp_batch_size

    assert clamp_batch_size(100) == 100
    assert clamp_batch_size(101) == 100
