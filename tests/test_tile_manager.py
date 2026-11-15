"""Tests for tile management and OpenStreetMap downloading."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from valhallaroute.tile_manager import (
    download_file,
    download_osm_pbf,
    ensure_tiles_exist,
    fetch_geofabrik_index,
    find_matching_geofabrik_region,
)


def test_download_file(tmp_path: Path):
    dest = tmp_path / "test.txt"
    mock_resp = MagicMock()
    mock_resp.headers = {"content-length": "12"}
    mock_resp.iter_content = MagicMock(return_value=[b"Hello ", b"World!"])

    with patch("requests.get", return_value=mock_resp):
        res = download_file("https://example.com/test.txt", dest, verbose=False)

    assert res == dest
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "Hello World!"


def test_fetch_geofabrik_index(tmp_path: Path):
    cache_file = tmp_path / "geofabrik.json"
    fake_index = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "id": "berlin",
                    "name": "Berlin",
                    "urls": {"pbf": "https://download.geofabrik.de/berlin.osm.pbf"},
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[13.0, 52.3], [13.8, 52.3], [13.8, 52.7], [13.0, 52.7], [13.0, 52.3]]],
                },
            }
        ],
    }

    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=fake_index)

    with patch("requests.get", return_value=mock_resp):
        data = fetch_geofabrik_index(cache_path=cache_file)

    assert data["features"][0]["properties"]["id"] == "berlin"
    assert cache_file.exists()

    # Second call should read from cache without network request
    with patch("requests.get") as mock_get:
        data2 = fetch_geofabrik_index(cache_path=cache_file)
        assert mock_get.call_count == 0
        assert data2 == fake_index


def test_find_matching_geofabrik_region():
    index = {
        "features": [
            {
                "properties": {"id": "europe", "name": "Europe", "urls": {"pbf": "http://europe.pbf"}},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-10.0, 35.0], [30.0, 35.0], [30.0, 70.0], [-10.0, 70.0], [-10.0, 35.0]]],
                },
            },
            {
                "properties": {"id": "berlin", "name": "Berlin", "urls": {"pbf": "http://berlin.pbf"}},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[13.0, 52.3], [13.8, 52.3], [13.8, 52.7], [13.0, 52.7], [13.0, 52.3]]],
                },
            },
        ]
    }

    coords = [(52.5200, 13.4050), (52.5210, 13.4060)]
    match = find_matching_geofabrik_region(coords, index=index)
    assert match is not None
    # Berlin is smaller than Europe, so Berlin should be chosen
    assert match["properties"]["id"] == "berlin"


def test_download_osm_pbf_with_existing_file(tmp_path: Path):
    existing_pbf = tmp_path / "local.osm.pbf"
    existing_pbf.write_bytes(b"dummy osm data")

    res = download_osm_pbf(dest_path=existing_pbf, verbose=False)
    assert res == existing_pbf


def test_download_osm_pbf_by_region(tmp_path: Path):
    fake_index = {
        "features": [
            {
                "properties": {
                    "id": "monaco",
                    "name": "Monaco",
                    "urls": {"pbf": "https://download.geofabrik.de/monaco.osm.pbf"},
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[7.4, 43.7], [7.5, 43.7], [7.5, 43.8], [7.4, 43.8], [7.4, 43.7]]],
                },
            }
        ]
    }

    with patch("valhallaroute.tile_manager.fetch_geofabrik_index", return_value=fake_index):
        with patch("valhallaroute.tile_manager.download_file") as mock_dl:
            expected_dest = tmp_path / "monaco.osm.pbf"
            mock_dl.return_value = expected_dest

            res = download_osm_pbf(
                region="monaco",
                output_dir=tmp_path,
                verbose=False,
            )

            assert res == expected_dest
            mock_dl.assert_called_once_with(
                "https://download.geofabrik.de/monaco.osm.pbf",
                expected_dest,
                verbose=False,
            )


def test_ensure_tiles_exist_already_present(tmp_path: Path):
    tar_file = tmp_path / "valhalla_tiles.tar"
    tar_file.write_bytes(b"fake tar data")

    res = ensure_tiles_exist(tile_extract=tar_file, verbose=False)
    assert res == tar_file


def test_ensure_tiles_exist_generation_pipeline(tmp_path: Path):
    tar_file = tmp_path / "valhalla_tiles.tar"
    pbf_file = tmp_path / "test.osm.pbf"
    pbf_file.write_bytes(b"fake pbf")

    with patch("valhallaroute.tile_manager.download_osm_pbf", return_value=pbf_file) as mock_dl:
        with patch("valhallaroute.tile_manager.build_valhalla_tiles", return_value=tar_file) as mock_build:
            res = ensure_tiles_exist(
                tile_extract=tar_file,
                osm_pbf=pbf_file,
                verbose=False,
            )

            assert res == tar_file
            mock_dl.assert_called_once()
            mock_build.assert_called_once()
