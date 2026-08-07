import core.client as client


def test_build_tag_search_body_uses_offset_in_param7_only():
    assert hasattr(client, "build_tag_search_body")

    body = client.build_tag_search_body("原神", offset=20, limit=20)

    assert "c0-param1=number:0" in body
    assert "c0-param6=number:20" in body
    assert "c0-param7=number:20" in body
    assert "c0-param8=number:0" in body
    assert "c0-param1=number:20" not in body
    assert "c0-param7=number:0" not in body
