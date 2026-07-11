from poly_data.ingest.markets import _parse_market, MARKET_COLUMNS


def test_market_columns_includes_category():
    assert "category" in MARKET_COLUMNS


def test_parse_market_extracts_category_from_events():
    raw = {
        "id": "1",
        "question": "Q",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["t1","t2"]',
        "createdAt": "2024-01-01T00:00:00Z",
        "events": [{"ticker": "trump-2024", "category": "Politics"}],
    }
    row = _parse_market(raw)
    assert row is not None
    assert row["category"] == "Politics"


def test_parse_market_falls_back_to_top_level_category():
    raw = {
        "id": "2",
        "question": "Q",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["t1","t2"]',
        "createdAt": "2024-01-01T00:00:00Z",
        "category": "Sports",
    }
    row = _parse_market(raw)
    assert row is not None
    assert row["category"] == "Sports"


def test_parse_market_empty_category_when_absent():
    raw = {
        "id": "3",
        "question": "Q",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["t1","t2"]',
        "createdAt": "2024-01-01T00:00:00Z",
    }
    row = _parse_market(raw)
    assert row is not None
    assert row["category"] == ""


def test_parse_market_preserves_official_resolution_metadata():
    raw = {
        "id": "4",
        "question": "Q",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["1","0"]',
        "clobTokenIds": '["t1","t2"]',
        "createdAt": "2024-01-01T00:00:00Z",
        "closed": True,
        "closedTime": "2024-02-01T00:00:00Z",
        "resolutionSource": "official-source",
        "umaResolutionStatus": "resolved",
    }

    row = _parse_market(raw)

    assert row is not None
    assert row["outcomePrices"] == '["1","0"]'
    assert row["closed"] is True
    assert row["resolutionSource"] == "official-source"
    assert row["umaResolutionStatus"] == "resolved"
