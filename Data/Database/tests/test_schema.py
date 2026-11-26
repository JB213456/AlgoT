from market_data_db.schema import Dataset, all_datasets, get_spec


def test_all_datasets_have_specs():
    for ds in all_datasets():
        spec = get_spec(ds)
        assert spec.name == ds
        names = set(spec.schema.names)
        for key in spec.primary_key:
            assert key in names
        assert "provider" in names
        assert "source" in names
        assert "ingested_at" in names
        assert spec.time_col in names


def test_get_spec_accepts_string():
    spec = get_spec("bars")
    assert spec.name == Dataset.BARS
