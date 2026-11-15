"""Sample execution script for ValhallaRoute."""
from __future__ import annotations

import io
from valhallaroute import enrich_gps_points


def main():
    # Sample GPS trip (in order of the trip)
    sample_csv = io.StringIO("""timestamp,latitude,longitude,speed_kmh
2026-11-15T10:00:00Z,52.5200,13.4050,45.0
2026-11-15T10:00:30Z,52.5205,13.4055,48.2
2026-11-15T10:01:00Z,52.5210,13.4060,50.1
""")

    print("Enriching sample trip using Valhalla...")
    # Demonstrating with public OSM Valhalla demo or local endpoint
    # You can also pass actor=Actor(...) or tile_extract="valhalla_tiles.tar"
    try:
        enriched_df = enrich_gps_points(
            sample_csv,
            valhalla_url="https://valhalla1.openstreetmap.de",
        )
        print("\nEnriched DataFrame:")
        print(enriched_df[[
            "timestamp", "latitude", "longitude",
            "is_tunnel", "is_bridge", "is_crossing",
            "city", "country", "road_name", "road_type"
        ]].to_string())
    except Exception as exc:
        print(f"Could not connect to online Valhalla: {exc}")
        print("You can pass tile_extract='path/to/tiles.tar' to run offline with local tiles.")


if __name__ == "__main__":
    main()
