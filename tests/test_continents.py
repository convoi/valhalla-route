"""Unit tests for continental tile pack builder."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from valhallaroute.cli import main
from valhallaroute.continents import (
    DEFAULT_CONTINENTS,
    build_continent_pack,
    normalize_continent_names,
)


def test_normalize_continent_names():
    # 1. Standard comma string
    assert normalize_continent_names("europe,north-america") == ["europe", "north-america"]

    # 2. Alias normalization ('oceania' -> 'australia-oceania')
    assert normalize_continent_names(["oceania", "south-america"]) == [
        "australia-oceania",
        "south-america",
    ]

    # 3. Default list on empty or 'all'
    assert normalize_continent_names("all") == [
        "europe",
        "north-america",
        "south-america",
        "australia-oceania",
    ]
    assert normalize_continent_names([]) == [
        "europe",
        "north-america",
        "south-america",
        "australia-oceania",
    ]

    # 4. Unknown continent raises ValueError
    with pytest.raises(ValueError, match="Unknown continent 'atlantis'"):
        normalize_continent_names("europe,atlantis")


def test_build_continent_pack_orchestration(tmp_path: Path):
    work_dir = tmp_path / "work"
    dest_dir = tmp_path / "dest"

    mock_download = MagicMock()
    mock_build_tiles = MagicMock()
    mock_build_admins = MagicMock()

    def fake_build_tiles(osm_pbf_path, tile_dir, tile_extract, admin_db_path, verbose):
        # Create fake .gph and .features.parquet in tile_dir
        t_dir = Path(tile_dir)
        (t_dir / "2" / "000" / "001").mkdir(parents=True, exist_ok=True)
        (t_dir / "2" / "000" / "001" / "123.gph").write_bytes(b"tile-data")
        (t_dir / "2" / "000" / "001" / "123.features.parquet").write_bytes(b"feature-data")

    def fake_build_admins(output_path, countries, admin_levels, verbose):
        Path(output_path).write_bytes(b"sqlite-admin-data")

    mock_build_tiles.side_effect = fake_build_tiles
    mock_build_admins.side_effect = fake_build_admins

    with patch("valhallaroute.continents.download_file", mock_download), \
         patch("valhallaroute.continents.build_valhalla_tiles", mock_build_tiles), \
         patch("valhallaroute.continents.build_admin_sqlite_from_overture", mock_build_admins):

        summary = build_continent_pack(
            continents=["oceania"],
            destination_storage_uri=str(dest_dir),
            build_admins=True,
            work_dir=work_dir,
            cleanup_after_continent=True,
            verbose=False,
        )

    # 1. Admin compilation was executed
    assert mock_build_admins.called

    # 2. Continent download and build was executed
    assert mock_download.called
    assert mock_build_tiles.called

    # 3. Summary structure validation
    assert summary["continents"] == ["australia-oceania"]
    assert summary["total_gph_tiles"] == 1
    assert summary["total_feature_parquet_tiles"] == 1
    assert "australia-oceania" in summary["by_continent"]

    # 4. Destination received uploaded files
    uploaded_gph = list(dest_dir.rglob("*.gph"))
    uploaded_parquet = list(dest_dir.rglob("*.features.parquet"))
    assert len(uploaded_gph) == 1
    assert len(uploaded_parquet) == 1


def test_cli_build_continents(tmp_path: Path):
    dest_dir = tmp_path / "cli_dest"

    with patch("valhallaroute.continents.build_continent_pack") as mock_pack:
        mock_pack.return_value = {"total_gph_tiles": 5}
        rc = main([
            "build-continents",
            "--continents", "oceania",
            "--destination-storage", str(dest_dir),
            "--no-build-admins",
        ])
        assert rc == 0
        assert mock_pack.called
        kwargs = mock_pack.call_args.kwargs
        assert kwargs["continents"] == "oceania"
        assert kwargs["destination_storage_uri"] == str(dest_dir)
        assert kwargs["build_admins"] is False
