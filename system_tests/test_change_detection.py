"""Verify dependency-aware CI selection and proprietary boundary."""
import importlib.util
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / '.github/actions/change-detection/detect.py'
spec = importlib.util.spec_from_file_location('change_detection', module_path)
detect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detect)


def test_database_interface_change_triggers_ingestion_and_system():
    result = detect.classify(['Data/Database/market_data_db/schema.py'])
    assert {key: result[key] for key in ('database', 'ingestion', 'system')} == {
        'database': True, 'ingestion': True, 'system': True,
    }


def test_ingestion_change_does_not_build_database():
    result = detect.classify(['Data/Data_Ingestion/Live/live_ingestion/engine.py'])
    assert {key: result[key] for key in ('database', 'ingestion', 'system')} == {
        'database': False, 'ingestion': True, 'system': True,
    }


def test_shared_workflow_triggers_both_active_services():
    result = detect.classify(['.github/workflows/reusable-ci.yml'])
    assert {key: result[key] for key in ('database', 'ingestion', 'system')} == {
        'database': True, 'ingestion': True, 'system': True,
    }


def test_proprietary_strategy_path_is_never_selected():
    result = detect.classify(['Strategies/private.py'])
    assert {key: result[key] for key in ('database', 'ingestion', 'system')} == {
        'database': False, 'ingestion': False, 'system': False,
    }


def test_future_service_source_triggers_its_gate():
    result = detect.classify(['Core/Feature_Engineering/pyproject.toml'])
    assert result['features'] is True
    assert result['database'] is False
