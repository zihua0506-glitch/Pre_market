from archiver.vm_export import _row_from_native_chunk


def test_row_extraction():
    chunk = {
        "metric": {"__name__": "pm_yes_mid", "market_id": "m1",
                   "city": "ZSPD", "contract_type": "high_ge"},
        "values": [[1715000000, "0.42"], [1715000003, "0.43"]],
    }
    rows = list(_row_from_native_chunk(chunk))
    assert len(rows) == 2
    assert rows[0] == {"ts": 1715000000, "metric": "pm_yes_mid",
                        "market_id": "m1", "city": "ZSPD",
                        "contract_type": "high_ge", "value": 0.42}
