import heapq
import json
import math
from collections import defaultdict
from functools import lru_cache

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage

MAX_ROAD_NETWORK_GEOJSON_BYTES = 100 * 1024 * 1024
MAX_ROUTE_SNAP_DISTANCE_KM = 0.5
_EXCLUDED_HIGHWAY_TYPES = frozenset({
    "bridleway",
    "construction",
    "corridor",
    "cycleway",
    "elevator",
    "footway",
    "pedestrian",
    "planned",
    "platform",
    "proposed",
    "raceway",
    "steps",
})


def _coordinate_key(value):
    try:
        longitude, latitude = float(value[0]), float(value[1])
    except (IndexError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return round(latitude, 7), round(longitude, 7)


def _distance_km(first, second):
    latitude1, longitude1 = map(math.radians, first)
    latitude2, longitude2 = map(math.radians, second)
    latitude_delta = latitude2 - latitude1
    longitude_delta = longitude2 - longitude1
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude1)
        * math.cos(latitude2)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(min(1, haversine)))


def road_graph_from_geojson(data):
    if (
        not isinstance(data, dict)
        or data.get("type") != "FeatureCollection"
        or not isinstance(data.get("features"), list)
    ):
        raise ValueError("Road data must be a GeoJSON FeatureCollection.")

    graph = defaultdict(dict)
    for feature in data["features"]:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        highway_type = properties.get("highway")
        if highway_type in _EXCLUDED_HIGHWAY_TYPES:
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "LineString":
            lines = [coordinates]
        elif geometry_type == "MultiLineString":
            lines = coordinates if isinstance(coordinates, list) else []
        else:
            continue

        oneway = str(properties.get("oneway", "")).lower()
        for line in lines:
            if not isinstance(line, list):
                continue
            points = [
                point
                for point in (_coordinate_key(value) for value in line)
                if point is not None
            ]
            for first, second in zip(points, points[1:]):
                distance = _distance_km(first, second)
                if distance <= 0:
                    continue
                graph[first][second] = min(
                    graph[first].get(second, float("inf")),
                    distance,
                )
                if oneway not in {"yes", "true", "1", "-1"}:
                    graph[second][first] = min(
                        graph[second].get(first, float("inf")),
                        distance,
                    )
                elif oneway == "-1":
                    graph[second][first] = min(
                        graph[second].get(first, float("inf")),
                        distance,
                    )
                    graph[first].pop(second, None)

    if not graph:
        raise ValueError("Road data contains no usable road segments.")
    return dict(graph)


def validate_road_network_geojson(upload):
    if getattr(upload, "_committed", False):
        return
    if upload.size > MAX_ROAD_NETWORK_GEOJSON_BYTES:
        raise ValidationError("Road network GeoJSON files must be 100 MB or smaller.")
    position = upload.tell()
    try:
        data = json.load(upload)
        road_graph_from_geojson(data)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValidationError(
            f"Invalid road network GeoJSON: {error}",
        ) from error
    finally:
        upload.seek(position)


@lru_cache(maxsize=1)
def _load_road_graph(storage_names):
    graph = defaultdict(dict)
    for storage_name in storage_names:
        with default_storage.open(storage_name, "rb") as road_file:
            road_graph = road_graph_from_geojson(json.load(road_file))
        for node, neighbors in road_graph.items():
            for neighbor, distance in neighbors.items():
                graph[node][neighbor] = min(
                    graph[node].get(neighbor, float("inf")),
                    distance,
                )
    if not graph:
        raise ValueError("No road network GeoJSON files were provided.")
    return dict(graph)


def _nearest_road_node(graph, coordinates):
    point = _coordinate_key((coordinates["lng"], coordinates["lat"]))
    if point is None:
        return None
    nearest_node = min(
        graph,
        key=lambda node: _distance_km(point, node),
    )
    snap_distance = _distance_km(point, nearest_node)
    if snap_distance > MAX_ROUTE_SNAP_DISTANCE_KM:
        return None
    return nearest_node, snap_distance


def local_road_route_distance_km(storage_name, origin, destination):
    if isinstance(storage_name, str):
        storage_names = (storage_name,) if storage_name else ()
    else:
        storage_names = tuple(
            name for name in (storage_name or ()) if name
        )
    if not storage_names or not origin or not destination:
        return None
    graph = _load_road_graph(storage_names)
    return road_route_distance_km(graph, origin, destination)


def road_route_distance_km(graph, origin, destination):
    if not graph or not origin or not destination:
        return None
    origin_node = _nearest_road_node(graph, origin)
    destination_node = _nearest_road_node(graph, destination)
    if not origin_node or not destination_node:
        return None
    start, origin_snap_distance = origin_node
    target, destination_snap_distance = destination_node
    if start == target:
        return origin_snap_distance + destination_snap_distance

    distances = {start: origin_snap_distance}
    queue = [(origin_snap_distance, start)]
    visited = set()
    while queue:
        distance, node = heapq.heappop(queue)
        if node in visited:
            continue
        if node == target:
            return distance + destination_snap_distance
        visited.add(node)
        for neighbor, edge_distance in graph.get(node, {}).items():
            if neighbor in visited:
                continue
            next_distance = distance + edge_distance
            if next_distance < distances.get(neighbor, float("inf")):
                distances[neighbor] = next_distance
                heapq.heappush(queue, (next_distance, neighbor))
    return None
