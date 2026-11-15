"""CLI tests for ValhallaRoute."""
from pathlib import Path
from unittest.mock import patch
import pandas as pd

from valhallaroute.cli import main


def test_cli_execution(tmp_path: Path):
    input_file = tmp_path / "trip.csv"
    output_file = tmp_path / "enriched.csv"

    input_file.write_text("latitude,longitude\n52.5200,13.4050\n52.5205,13.4055\n", encoding="utf-8")

    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [{"names": ["Sample Road"], "road_class": "secondary", "admin_index": 0}],
        "matched_points": [{"type": "matched", "edge_index": 0}, {"type": "matched", "edge_index": 0}],
    }

    with patch("valhallaroute.client.HttpBackend.trace_attributes", return_value=mock_resp):
        exit_code = main([
            str(input_file),
            "-o", str(output_file),
            "--url", "http://fake-valhalla:8002",
        ])

    assert exit_code == 0
    assert output_file.exists()

    df = pd.read_csv(output_file)
    assert len(df) == 2
    assert "valhalla_type" in df.columns
    assert "is_on_ramp" in df.columns
    assert "is_exit_ramp" in df.columns
    assert "is_tunnel" in df.columns
    assert "is_bridge" in df.columns
    assert "is_crossing" in df.columns
    assert "city" in df.columns
    assert "country" in df.columns
    assert "road_name" in df.columns
    assert "road_type" in df.columns
    assert df.loc[0, "road_name"] == "Sample Road"
    assert df.loc[0, "valhalla_type"] == "matched"
